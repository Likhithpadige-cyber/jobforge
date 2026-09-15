"""Operations models (DATABASE_DESIGN.md §7).

``events`` and ``audit_logs`` are append-only: no role holds a DELETE grant,
because they are the user's evidence of their own conduct.
"""

from __future__ import annotations

from datetime import date as dt_date  # aliased: DailyQuota has a `date` column
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base, UUIDPrimaryKeyMixin
from packages.core.models.enums import (
    AgentName,
    AlertSeverity,
    RunStatus,
    agent_name_enum,
    alert_severity_enum,
    run_status_enum,
)


class Event(Base):
    """Transactional outbox and audit log (DATABASE_DESIGN.md §7.1, EVENTS.md §2).

    Written in the same transaction as the state change it describes; the
    outbox relay publishes unpublished rows to Redis afterwards.
    """

    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    correlation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    published_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("actor_type IN ('agent', 'human', 'system')", name="actor_type_allowed"),
        CheckConstraint(
            "aggregate_type IN ('job','application','resume','approval','submission','system')",
            name="aggregate_type_allowed",
        ),
        Index(
            "idx_events_unpublished",
            "occurred_at",
            postgresql_where=sa_text("published = false"),
        ),
        Index("idx_events_aggregate", "aggregate_type", "aggregate_id",
              sa_text("occurred_at DESC")),
        Index("idx_events_correlation", "correlation_id", "occurred_at"),
        Index("idx_events_type_time", "type", sa_text("occurred_at DESC")),
    )


class ProcessedEvent(Base):
    """Idempotency ledger. Composite PK of (event_id, consumer_group)."""

    __tablename__ = "processed_events"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    consumer_group: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    result: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        PrimaryKeyConstraint("event_id", "consumer_group", name="pk_processed_events"),
        Index("ix_processed_events_processed_at", "processed_at"),
    )


class AgentRun(Base):
    """One agent invocation, with cost and token accounting."""

    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent: Mapped[AgentName] = mapped_column(agent_name_enum, nullable=False)
    pipeline: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_event_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")
    )
    aggregate_type: Mapped[str | None] = mapped_column(Text)
    aggregate_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    correlation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[RunStatus] = mapped_column(
        run_status_enum, nullable=False, server_default="running"
    )
    steps_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    steps_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default="0"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None]

    steps: Mapped[list[AgentRunStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "pipeline IN ('evaluation','tailoring','packet','submission','digest')",
            name="pipeline_allowed",
        ),
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
        CheckConstraint("steps_completed <= steps_total OR steps_total = 0",
                        name="steps_consistent"),
        Index("ix_agent_runs_agent_started", "agent", sa_text("started_at DESC")),
        Index("ix_agent_runs_correlation_id", "correlation_id"),
    )


class AgentRunStep(UUIDPrimaryKeyMixin, Base):
    """Step-level record, so an interrupted run resumes instead of restarting."""

    __tablename__ = "agent_run_steps"

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        run_status_enum, nullable=False, server_default="running"
    )
    input_digest: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    guardrail_results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None]

    run: Mapped[AgentRun] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_agent_run_steps_run_id_step_index"),
    )


class LlmCall(UUIDPrimaryKeyMixin, Base):
    """Per-call cost accounting. Prompt/response are stored redacted, 30 days."""

    __tablename__ = "llm_calls"

    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    step_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_run_steps.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default="0"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    prompt_redacted: Mapped[str | None] = mapped_column(Text)
    response_redacted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
        Index("ix_llm_calls_created_at", sa_text("created_at DESC")),
    )


class DailyQuota(UUIDPrimaryKeyMixin, Base):
    """Durable quota ledger. Redis holds the fast counter; this is the truth."""

    __tablename__ = "daily_quotas"

    date: Mapped[dt_date] = mapped_column(Date, nullable=False)
    quota_type: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    used_value: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("date", "quota_type", name="uq_daily_quotas_date_quota_type"),
        CheckConstraint(
            "quota_type IN ('applications','llm_cost','evaluations','submissions')",
            name="quota_type_allowed",
        ),
        CheckConstraint("used_value >= 0 AND limit_value >= 0", name="values_non_negative"),
    )


class MetricsSnapshot(UUIDPrimaryKeyMixin, Base):
    """Pre-computed dashboard aggregates. Written by Agent 3 only."""

    __tablename__ = "metrics_snapshots"

    grain: Mapped[str] = mapped_column(String(8), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("grain IN ('5min', 'hour', 'day')", name="grain_allowed"),
        UniqueConstraint("grain", "bucket_start", name="uq_metrics_snapshots_grain_bucket_start"),
    )


class Alert(UUIDPrimaryKeyMixin, Base):
    """``auto_paused`` marks the fabrication pause described in SECURITY.md §4.7."""

    __tablename__ = "alerts"

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(alert_severity_enum, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    aggregate_type: Mapped[str | None] = mapped_column(Text)
    aggregate_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    auto_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    acknowledged_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledged_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')", name="status_allowed"
        ),
        Index("idx_alerts_open", sa_text("created_at DESC"),
              postgresql_where=sa_text("status = 'open'")),
    )


class Notification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notifications"

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    read_at: Mapped[datetime | None]
    dispatched_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("channel IN ('in_app', 'email')", name="channel_allowed"),
    )


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """Populated by database triggers, so changes are captured even when they
    bypass the application layer. Insert-only for every role."""

    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("action IN ('insert', 'update', 'delete')", name="action_allowed"),
        Index("idx_audit_logs_resource", "resource_type", "resource_id",
              sa_text("created_at DESC")),
    )


class Setting(Base):
    """Runtime configuration. ``GRANT SELECT`` only to agent roles."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
