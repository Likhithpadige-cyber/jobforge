"""Durable C3 revision attempts (C3_HARDENING_DESIGN.md A.1).

One row per rework request. It is created when a human asks for changes, it
outlives the generation it describes, and it is never deleted - which is the
whole point of the durable-attempt model (PO-4): a failed generation must not
erase the evidence that it happened, what it cost, or why it failed.

Two axes are kept rigorously apart, and confusing them is the failure mode
this table exists to prevent:

``status``   is about the JOB - did the work run to completion?
``outcome``  is about the CONTENT - did the produced resume verify?

A B5 negative verdict is ``status='succeeded', outcome='failed_verification'``:
the job did exactly what it should, and v2 exists as a durable failed record.
``ck_attempt_outcome_only_on_success`` makes that a database fact rather than
a convention, so a semantic negative can never wear a ``failed`` status.

Guarded by two BEFORE UPDATE triggers created in migration 0005:

``jobforge_revision_attempt_source_immutable``
    source bindings never move; retry history only moves forward; a
    ``succeeded`` row is frozen entirely and an ``abandoned`` row accepts only
    ``replaced_by_attempt_id``, once, from NULL.
``jobforge_revision_attempt_transition``
    any ``status`` change absent from the C.1 lifecycle table is refused.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, Text, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.core.models.enums import (
    RevisionAttemptStatus,
    RevisionFailureClass,
    RevisionOutcome,
    revision_attempt_status_enum,
    revision_failure_class_enum,
    revision_outcome_enum,
)


class ResumeRevisionAttempt(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """One rework request, from acceptance to a terminal state."""

    __tablename__ = "resume_revision_attempts"

    # --- immutable source binding -------------------------------------
    # Which cycle this attempt belongs to. The trigger refuses any change to
    # these: an attempt that could be re-pointed at another approval would
    # make "one live attempt per approval" unenforceable.
    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_resume_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("resumes.id", ondelete="NO ACTION"),
        nullable=False,
    )
    #: Denormalised on purpose: T3 compares against the version it was told
    #: about, not the version the row happens to hold at commit time.
    source_resume_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_approval_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("approvals.id", ondelete="NO ACTION"),
        nullable=False,
    )
    #: The screened note, exactly as sent to B1, and its digest.
    instructions_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    instructions_digest: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    # --- lifecycle ----------------------------------------------------
    status: Mapped[RevisionAttemptStatus] = mapped_column(
        revision_attempt_status_enum, nullable=False, server_default="accepted"
    )
    #: Load-bearing, not diagnostics: part of the renewal (D.5) and failure
    #: report (D.6) predicates.
    lease_owner: Mapped[str | None] = mapped_column(Text)
    #: The fence. A takeover increments it; a fenced-out worker matches 0 rows.
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_expires_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    # --- result -------------------------------------------------------
    result_resume_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("resumes.id", ondelete="NO ACTION")
    )
    result_version: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[RevisionOutcome | None] = mapped_column(revision_outcome_enum)

    # --- failure ------------------------------------------------------
    failure_class: Mapped[RevisionFailureClass | None] = mapped_column(
        revision_failure_class_enum
    )
    #: Never model prose. A classification and a short operator-facing note.
    failure_detail: Mapped[str | None] = mapped_column(Text)

    # --- retry accounting ---------------------------------------------
    #: Incremented in exactly one place (D.2 acquisition), never here.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    retry_exhausted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: Durable record of how many evidence_stale retries this row has spent.
    #: Monotonic by trigger: never reset, never decremented, never cleared by
    #: a later failure_class write. Without it, an attempt that consumed its
    #: retry and then failed with model_timeout could be re-reported as
    #: evidence_stale and silently earn a second one.
    evidence_stale_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    evidence_stale_retried_at: Mapped[datetime | None]

    # --- lineage and recovery -----------------------------------------
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    replaces_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("resume_revision_attempts.id", ondelete="NO ACTION"),
    )
    replaced_by_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("resume_revision_attempts.id", ondelete="NO ACTION"),
    )
    #: Predecessor + 1. Non-negative in the schema; the ceiling is
    #: configuration (N), enforced in the service, never a CHECK - raising a
    #: limit must not require a migration.
    replacement_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    abandoned_at: Mapped[datetime | None]
    #: Abandonment always names a human (PO-3). The worker cannot write it:
    #: it is absent from app_agent's column-level UPDATE grant.
    abandoned_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    abandon_reason: Mapped[str | None] = mapped_column(Text)
    #: Set by G.6 recovery, once, never by the worker.
    recovered_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "status <> 'succeeded' OR (result_resume_id IS NOT NULL "
            "AND result_version IS NOT NULL AND outcome IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="succeeded_requires_result",
        ),
        CheckConstraint(
            "status <> 'failed' OR failure_class IS NOT NULL",
            name="failed_requires_class",
        ),
        CheckConstraint(
            "status NOT IN ('accepted','running') "
            "OR (result_resume_id IS NULL AND result_version IS NULL "
            "AND outcome IS NULL)",
            name="open_has_no_result",
        ),
        CheckConstraint(
            "outcome IS NULL OR status = 'succeeded'",
            name="outcome_only_on_success",
        ),
        CheckConstraint(
            "status <> 'abandoned' "
            "OR (abandoned_at IS NOT NULL AND abandoned_by IS NOT NULL)",
            name="abandoned_named_a_human",
        ),
        CheckConstraint(
            "status <> 'running' "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        CheckConstraint(
            "replacement_ordinal >= 0",
            name="replacement_ordinal_non_negative",
        ),
        CheckConstraint(
            "evidence_stale_retry_count >= 0 "
            "AND (evidence_stale_retry_count = 0) "
            "= (evidence_stale_retried_at IS NULL)",
            name="evidence_stale_retry_coherent",
        ),
        Index(
            "uq_revision_attempt_per_approval",
            "source_approval_id",
            unique=True,
            postgresql_where=sa_text("status <> 'abandoned'"),
        ),
        Index(
            "idx_revision_attempts_claimable",
            "lease_expires_at",
            postgresql_where=sa_text("status IN ('accepted', 'running')"),
        ),
        Index("ix_revision_attempts_application_id", "application_id"),
    )
