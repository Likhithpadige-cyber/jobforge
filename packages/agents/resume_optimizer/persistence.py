"""A5 — evaluation persistence, status transition, event emission.

AGENT_DESIGN.md §3.3 step A5:

    Persist `job_evaluations`; transition job to `shortlisted` or `discarded`
    against thresholds in `settings`; emit `job.evaluated`.

**No LLM call exists in this module.** A5 takes an already-computed result
(from A2/A3) and records it. It is the deterministic tail of the evaluation
pipeline, and keeping it free of inference is what lets it be tested against a
real database without a model.

Everything happens in the caller's transaction: the evaluation row, the status
change and the outbox events commit together or not at all (ARCHITECTURE.md
§3.4). A committed status change with a lost event is a job that never reaches
the queue and never explains why.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.resume_optimizer.prefilter import ScoreBreakdown
from packages.agents.resume_optimizer.rationale import build_rationale
from packages.core.events import emit
from packages.core.models import Job, JobEvaluation, JobStatus, Recommendation
from packages.core.repositories import JobRepository
from packages.core.state_machines import transition
from packages.shared.errors import NotFoundError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

log = get_logger(__name__)

CONSUMER_GROUP = "cg.optimizer"


@dataclass(frozen=True)
class Thresholds:
    """Band edges from settings (AGENT_DESIGN.md §3.3).

    `review_floor` is why there are three outcomes rather than two: a score
    between the floor and the shortlist threshold is *borderline*, and the
    design says those are "presented to the human as a borderline batch
    rather than auto-discarded". Such a job stays at `evaluated`, which is
    precisely the state ARCHITECTURE.md §7.1 defines as awaiting a threshold
    or human decision.
    """

    shortlist: Decimal = Decimal("65")
    review_floor: Decimal = Decimal("55")

    @classmethod
    def from_settings(cls, settings: object) -> Thresholds:
        return cls(
            shortlist=Decimal(str(getattr(settings, "fit_score_shortlist_threshold", 65))),
            review_floor=Decimal(str(getattr(settings, "fit_score_review_floor", 55))),
        )

    def __post_init__(self) -> None:
        if self.review_floor > self.shortlist:
            raise ValidationError(
                f"review_floor ({self.review_floor}) cannot exceed the shortlist "
                f"threshold ({self.shortlist})"
            )


@dataclass
class PersistResult:
    evaluation_id: UUID
    job_id: UUID
    from_status: JobStatus
    to_status: JobStatus
    score: Decimal
    recommendation: Recommendation
    events_emitted: list[str]
    created: bool = True

    @property
    def borderline(self) -> bool:
        """Scored above the discard floor but below auto-shortlist."""
        return self.to_status is JobStatus.EVALUATED


def decide_status(score: Decimal, thresholds: Thresholds) -> tuple[JobStatus, str | None]:
    """Map a score onto the documented outcome. Pure.

    Three outcomes, not two - see `Thresholds`.
    """
    if score >= thresholds.shortlist:
        return JobStatus.SHORTLISTED, None
    if score >= thresholds.review_floor:
        return JobStatus.EVALUATED, None
    return JobStatus.DISCARDED, (
        f"score {score} is below the review floor {thresholds.review_floor}"
    )


async def persist_evaluation(
    session: AsyncSession,
    *,
    job_id: UUID,
    profile_version: int,
    breakdown: ScoreBreakdown,
    match_matrix: dict[str, object],
    thresholds: Thresholds | None = None,
    correlation_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    rationale: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    causation_id: UUID | None = None,
    trace_id: str | None = None,
) -> PersistResult:
    """Record an evaluation, move the job, and emit the events.

    `rationale` is optional. When it is omitted, A4 derives one deterministically
    from the matrix and the score - both already in hand, so this adds no input
    and no inference. An explicitly supplied rationale is stored as given, so
    existing callers are unaffected.

    `model` and `prompt_version` stay optional and stay null on this path:
    nothing here uses a model, and recording one would misdescribe how the
    evaluation was produced.
    """
    thresholds = thresholds or Thresholds()
    jobs = JobRepository(session)

    job: Job | None = await jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found")

    correlation_id = correlation_id or job.id

    # Evaluations are append-only and immutable (DATABASE_DESIGN.md §5.6):
    # re-evaluation creates a NEW row rather than overwriting. There is
    # deliberately no unique constraint to violate, so idempotency is the
    # caller's contract via processed_events - see `persist_evaluation_once`.
    # A4: explain the score that A3 already computed. Derived, never inferred -
    # every sentence is assembled from counts and text in the two inputs, so
    # the prose can be rebuilt from the stored row at any time.
    if rationale is None:
        rationale = build_rationale(
            match_matrix,
            score=breakdown.score,
            recommendation=str(breakdown.recommendation),
            gaps=list(breakdown.gaps),
        ).summary

    evaluation = JobEvaluation(
        id=uuid7(),
        job_id=job.id,
        profile_version=profile_version,
        score=breakdown.score,
        recommendation=breakdown.recommendation,
        match_matrix=match_matrix,
        matched_requirements={
            "count": sum(
                1 for row in match_matrix.get("requirements", [])  # type: ignore[union-attr]
                if isinstance(row, dict) and row.get("status") in ("met", "partially_met")
            )
        },
        missing_requirements={"count": len(breakdown.gaps)},
        gaps=list(breakdown.gaps),
        rationale=rationale,
        score_breakdown=breakdown.as_dict(),
        agent_run_id=agent_run_id,
        model=model,
        prompt_version=prompt_version,
    )
    session.add(evaluation)
    await session.flush()

    # The lifecycle is normalized -> evaluated -> shortlisted|discarded
    # (ARCHITECTURE.md §7.1). Jumping straight to the threshold outcome skips
    # `evaluated`, which is not a documented edge - and `evaluated` is not a
    # formality: it is the state meaning "a fit score exists", which is true
    # the moment the evaluation row is written, regardless of what the
    # threshold then decides.
    #
    # Every move goes through transition(); job.status is never assigned here.
    from_status = transition(job, JobStatus.EVALUATED)
    await session.flush()

    emitted: list[str] = []

    # job.evaluated always fires - it is the record that an evaluation
    # happened, independent of what the threshold decided.
    await emit(
        session,
        type="job.evaluated",
        aggregate_type="job",
        aggregate_id=job.id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        trace_id=trace_id,
        actor_type="agent",
        actor_id="resume_optimizer",
        payload={
            "job_id": str(job.id),
            "evaluation_id": str(evaluation.id),
            "score": float(breakdown.score),
            "recommendation": str(breakdown.recommendation),
            "matched_count": evaluation.matched_requirements["count"],
            "missing_count": evaluation.missing_requirements["count"],
            "gaps": list(breakdown.gaps),
            "agent_run_id": str(agent_run_id) if agent_run_id else None,
        },
    )
    emitted.append("job.evaluated")

    # Only now does the threshold decide whether the job moves further. A
    # borderline score leaves it at `evaluated`, awaiting a human, and emits
    # nothing more.
    target, discard_reason = decide_status(breakdown.score, thresholds)
    if target is not JobStatus.EVALUATED:
        transition(job, target, reason=discard_reason)
        await session.flush()

    # The transition event, when the threshold actually moved the job.
    # EVENTS.md §3.1 lists both as produced by A1; without them the queue and
    # the monitor have no signal that anything changed.
    if target is JobStatus.SHORTLISTED:
        await emit(
            session,
            type="job.shortlisted",
            aggregate_type="job",
            aggregate_id=job.id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace_id=trace_id,
            actor_type="agent",
            actor_id="resume_optimizer",
            payload={
                "job_id": str(job.id),
                "evaluation_id": str(evaluation.id),
                "score": float(breakdown.score),
                "by": "agent",
            },
        )
        emitted.append("job.shortlisted")
    elif target is JobStatus.DISCARDED:
        await emit(
            session,
            type="job.discarded",
            aggregate_type="job",
            aggregate_id=job.id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace_id=trace_id,
            actor_type="agent",
            actor_id="resume_optimizer",
            payload={
                "job_id": str(job.id),
                "reason": discard_reason,
                "stage": "evaluation",
                "score": float(breakdown.score),
            },
        )
        emitted.append("job.discarded")

    log.info(
        "evaluation.persisted",
        job_id=str(job.id),
        evaluation_id=str(evaluation.id),
        score=float(breakdown.score),
        from_status=str(from_status),
        to_status=str(target),
        events=emitted,
    )

    return PersistResult(
        evaluation_id=evaluation.id,
        job_id=job.id,
        from_status=from_status,
        to_status=job.status,
        score=breakdown.score,
        recommendation=breakdown.recommendation,
        events_emitted=emitted,
    )


async def persist_evaluation_once(
    session: AsyncSession,
    *,
    triggering_event_id: UUID,
    consumer_group: str = CONSUMER_GROUP,
    **kwargs: object,
) -> PersistResult | None:
    """Idempotent wrapper for event-driven callers.

    Delivery is at-least-once (EVENTS.md §4.1), so a redelivered
    `job.evaluation.requested` must not produce a second evaluation row and a
    second `job.evaluated`.

    Algorithm, in this order:

        1. claim (event_id, consumer_group) atomically via
           INSERT ... ON CONFLICT DO NOTHING
        2. if the insert affected zero rows, someone else has it -> return None
        3. otherwise do the work
        4. annotate the claim with the outcome

    The claim happens **before** any work and its success is decided by the
    INSERT itself. An earlier version of this function did SELECT-then-INSERT,
    which looks equivalent and is not: two concurrent deliveries both read
    "not processed", both proceed, and the job is evaluated twice with two
    `job.evaluated` events.

    The claim is written in the caller's transaction, so a failure downstream
    rolls the claim back too and a legitimate retry can re-claim. A claim that
    survived a failed handler would strand the event permanently.

    Genuine re-evaluation is unaffected: a *different* triggering event, or a
    newer `profile_version`, still produces a new append-only evaluation row.
    """
    from packages.core.events import claim_event, record_claim_result

    if not await claim_event(session, triggering_event_id, consumer_group):
        log.info(
            "evaluation.skipped.already_claimed",
            event_id=str(triggering_event_id),
            consumer_group=consumer_group,
        )
        return None

    result = await persist_evaluation(session, **kwargs)  # type: ignore[arg-type]
    await record_claim_result(
        session, triggering_event_id, consumer_group, str(result.to_status)
    )
    return result
