"""Event envelope and type registry (EVENTS.md §2, §3).

The envelope is transport-agnostic on purpose: nothing here is Redis-specific,
so moving to SQS later is an adapter swap rather than a rewrite (EVENTS.md §1
principle 5).

Only the types listed in `KNOWN_EVENT_TYPES` may be emitted. An unregistered
type raises rather than being written, because EVENTS.md §2 says unknown types
are DLQ'd and never guessed at - and a producer that can invent a type name
would silently create events no consumer subscribes to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from packages.shared.clock import utc_now
from packages.shared.ids import uuid7

ActorType = Literal["agent", "human", "system"]
AggregateType = Literal["job", "application", "resume", "approval", "submission", "system"]

#: The catalogue from EVENTS.md §3. Kept as a flat set: the CI contract test
#: in tests/unit asserts this matches the document.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # jobs
        "job.discovered", "job.normalized", "job.duplicate_detected",
        "job.evaluation.requested", "job.evaluated", "job.shortlisted",
        "job.discarded", "job.expired",
        # resumes
        "resume.master.imported", "resume.first_draft.requested",
        "resume.generation.requested", "resume.generated",
        "resume.verification.failed", "resume.render.requested", "resume.rendered",
        # M10 Phase 1. The request/result pair for cover-letter (re)generation,
        # matching the resume.render.requested/application.questions.requested
        # shape: an owner-review-triggered request event with its own
        # idempotency/replay through the worker machinery, not a bare
        # in-process call. Emitted both on first generation (from
        # resume_review.enter_review) and on every regeneration after a
        # reviewer requests changes.
        "cover_letter.generation.requested",
        "cover_letter.generated",
        # applications
        "application.created",
        # M9. The request/result pair for question extraction, matching the
        # existing resume.first_draft.requested/resume.render.requested
        # shape: an owner-initiated trigger with its own event, not a bare
        # API-layer call, so the worker/idempotency/replay machinery already
        # built for those two pipelines applies here unchanged.
        "application.questions.requested",
        "application.questions.extracted",
        "application.answers.drafted", "application.ready",
        "application.status.changed", "application.outcome.updated",
        "application.abandoned",
        # approvals
        "approval.requested", "approval.granted", "approval.rejected",
        "approval.changes_requested", "approval.superseded",
        # submissions
        "submission.requested", "submission.retry_requested", "submission.started",
        "submission.succeeded", "submission.failed", "submission.handoff.required",
        # system
        "agent.run.started", "agent.run.finished", "agent.run.failed",
        "worker.delivery.started",
        "guardrail.violation.detected", "policy.blocked", "quota.exhausted",
        "metrics.snapshot.created", "alert.raised", "alert.resolved",
        "notification.dispatched", "schedule.tick",
        # Inference evidence failures; no stage lifecycle events.
        "inference.audit.start_failed",
        "inference.audit.evidence_not_recorded",
        "inference.audit.outcome_not_recorded",
        # C3 revision-attempt diagnostics (C3_HARDENING_DESIGN Batch 3A).
        # Written by the audit boundary on its own connection so they survive
        # the rollback they usually describe; registered here because the
        # catalogue is the contract for what may exist in `events` at all,
        # independent of which writer produced a row.
        "revision.attempt.failure_record_failed",
        "revision.attempt.fenced_out_before_failure",
        "revision.attempt.failure_not_recorded",
        "revision.attempt.success_not_recorded",
        "revision.attempt.recovery_failed",
        "revision.attempt.wait_timed_out",
        "revision.attempt.reconciled",
        "revision.attempt.reconcile_skipped",
    }
)

#: Delivery compatibility, not a payload migration. Historical v1 remains
#: supported; only resume.rendered has a canonical v2 producer today.
SUPPORTED_EVENT_VERSIONS: dict[str, frozenset[int]] = {
    event_type: frozenset({1, 2}) if event_type == "resume.rendered" else frozenset({1})
    for event_type in KNOWN_EVENT_TYPES
}

#: Events a human alone may produce (EVENTS.md §3.4). An agent emitting one of
#: these would mean an approval had been forged.
HUMAN_ONLY_EVENT_TYPES: frozenset[str] = frozenset(
    {"approval.granted", "approval.rejected", "approval.changes_requested",
     "resume.master.imported", "resume.first_draft.requested", "submission.requested"}
)


class UnknownEventTypeError(ValueError):
    """Raised when a producer tries to emit a type not in the catalogue."""


class ForbiddenActorError(ValueError):
    """Raised when an agent tries to emit a human-only event."""


@dataclass(frozen=True)
class EventEnvelope:
    """One event, exactly as EVENTS.md §2 specifies."""

    type: str
    aggregate_type: AggregateType
    payload: dict[str, Any]
    correlation_id: UUID
    actor_type: ActorType
    actor_id: str | None = None
    aggregate_id: UUID | None = None
    causation_id: UUID | None = None
    trace_id: str | None = None
    version: int = 1
    id: UUID = field(default_factory=uuid7)
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.type not in KNOWN_EVENT_TYPES:
            raise UnknownEventTypeError(
                f"{self.type!r} is not in the EVENTS.md catalogue; add it there first"
            )
        if self.type in HUMAN_ONLY_EVENT_TYPES and self.actor_type != "human":
            raise ForbiddenActorError(
                f"{self.type!r} may only be emitted by a human actor, "
                f"not {self.actor_type!r}"
            )

    def as_row(self) -> dict[str, Any]:
        """Column mapping for the `events` table (DATABASE_DESIGN.md §7.1)."""
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "trace_id": self.trace_id,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }
