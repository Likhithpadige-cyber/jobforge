"""B6 - resume persistence, status transition, event emission.

AGENT_DESIGN.md §3.4 step B6:

    Persist `resumes` + `resume_claims` with verdicts. Any `unsupported` or
    `overreach` -> `resume.verification.failed`, application to
    `verification_failed`, human review. Otherwise emit `resume.generated`
    and request rendering.

**No LLM call exists in this module.** B6 takes the already-generated claims
and the already-issued verdicts and records them. It is the deterministic tail
of Pipeline B, exactly as A5 is for Pipeline A, and keeping inference out of it
is what lets it be tested against a real database without a model.

B6 never re-judges a claim. It consumes only the validated B5 handoff, so the
pairing of verdict to claim is the one `validate_coverage` proved - never one
B6 reconstructed by position or similarity.

A failed verification is a durable outcome, not a discarded one: the resume and
every claim are written with their verdicts and reasons so a human can see the
sentence that failed and why. Everything - resume, claims, status, history and
outbox events - lands in the caller's transaction together.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select

from packages.agents.resume_optimizer.handoff import (
    HandoffError,
    VerifiedClaim,
    validate_coverage,
)
from packages.core.events import emit
from packages.core.models import (
    Application,
    ApplicationStatus,
    ClaimVerdict,
    Resume,
    ResumeClaim,
)
from packages.core.state_machines.application import transition_application
from packages.shared.errors import NotFoundError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.agents.resume_optimizer.context import TailoringContext
    from packages.agents.resume_optimizer.drafting import DraftedBullet
    from packages.agents.resume_optimizer.selection import SelectionResult
    from packages.agents.resume_optimizer.summary import SummarySentence

log = get_logger(__name__)

CONSUMER_GROUP = "cg.optimizer"

#: `resumes.verification_status` values, per the ck_resumes_verification_status
#: CHECK. `pending` is the schema default and never written by B6: by the time
#: B6 runs, every claim has a verdict.
PASSED = "passed"
FAILED = "failed"

#: EVENTS.md §3.2 - the renderer consumes this and produces both artifacts.
RENDER_FORMATS = ["pdf", "docx"]


class WrongApplicationStateError(ValidationError):
    """B6 was entered with the application outside `resume_generating`."""


@dataclass(frozen=True)
class PersistResult:
    resume_id: UUID
    version: int
    verification_status: str
    to_status: ApplicationStatus
    claim_count: int
    verified_count: int
    emitted: tuple[str, ...]


def _failures(verified: tuple[VerifiedClaim, ...]) -> list[dict[str, Any]]:
    """The failure detail EVENTS.md §3.2 requires on the tripwire event.

    This is the one payload that carries claim prose, and it does so because
    the document specifies it: a human has to see which sentence failed and
    why without opening a log.
    """
    return [
        {
            "path": item.claim.path,
            "claim_text": item.claim.claim_text,
            "verdict": str(item.verdict),
            "reason": item.explanation,
            "fact_ids": list(item.claim.fact_ids),
        }
        for item in verified
        if item.is_negative
    ]


def build_content(verified: tuple[VerifiedClaim, ...]) -> dict[str, Any]:
    """Structured resume content whose shape matches the claim paths.

    `ClaimRef.path` is `/<section>/<index>` with a run-wide index, so each
    section is stored as an index-keyed object rather than an array: that is
    what makes the persisted pointer actually resolve into this document.
    DATABASE_DESIGN.md §6.2 requires `content` to be structured precisely so
    the renderer, the diff view and the claim table cannot drift apart.
    """
    content: dict[str, Any] = {}
    for item in verified:
        section = content.setdefault(item.claim.section, {})
        index = item.claim.path.rsplit("/", 1)[-1]
        section[index] = {
            "text": item.claim.claim_text,
            "fact_ids": list(item.claim.fact_ids),
            "numerals_used": list(item.claim.numerals_used),
            "order": int(index),
        }
    return content


def build_verification_report(verified: tuple[VerifiedClaim, ...]) -> dict[str, Any]:
    """Per-claim verdicts and reasons (DATABASE_DESIGN.md §6.2)."""
    return {
        "claims": [
            {
                "path": item.claim.path,
                "section": item.claim.section,
                "verdict": str(item.verdict),
                "reason": item.explanation,
                "verified_at": item.verified_at.isoformat(),
                "input_digest": item.input_digest,
            }
            for item in verified
        ],
        "supported": sum(1 for item in verified if not item.is_negative),
        "failed": sum(1 for item in verified if item.is_negative),
    }


async def _next_version(session: AsyncSession, application_id: UUID) -> int:
    """The next append-only version for this application.

    The application row is locked by the caller before this runs, which is what
    serializes two concurrent generation attempts; `resumes` has no unique
    constraint on `(application_id, version)` to fall back on.
    """
    current = (
        await session.execute(
            select(func.max(Resume.version)).where(
                Resume.application_id == application_id, Resume.kind == "tailored"
            )
        )
    ).scalar_one_or_none()
    return (current or 0) + 1


async def persist_resume(
    session: AsyncSession,
    *,
    application_id: UUID,
    verified: tuple[VerifiedClaim, ...] | list[VerifiedClaim],
    bullets: tuple[DraftedBullet, ...],
    sentences: tuple[SummarySentence, ...],
    context: TailoringContext,
    selection: SelectionResult,
    correlation_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    cost_usd: Decimal | None = None,
    request_rendering: bool = True,
    actor_id: str = "resume_optimizer",
    causation_id: UUID | None = None,
    trace_id: str | None = None,
) -> PersistResult:
    """Record one generation attempt, move the application, emit the events."""
    # 1. The handoff gate first. Nothing is written until the verdicts are
    #    proven to describe exactly these claims - no missing, duplicate,
    #    orphaned or stale result, and no reconstruction by B6.
    ordered = validate_coverage(
        verified,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )

    # Defence in depth: `validate_coverage` already refuses `not_checked`, but
    # a claim reaching the database without a verdict is the one failure this
    # table must never record, so B6 checks again rather than trusting upstream.
    if any(item.verdict is ClaimVerdict.NOT_CHECKED for item in ordered):
        raise HandoffError("B6 will not persist a claim with verdict not_checked")

    # 2. Lock the application row: this both loads the current state and
    #    serializes concurrent attempts while the next version is chosen.
    application = (
        await session.execute(
            select(Application)
            .where(Application.id == application_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"Application {application_id} does not exist")
    if application.status is not ApplicationStatus.RESUME_GENERATING:
        raise WrongApplicationStateError(
            f"B6 requires an application in resume_generating, not {application.status}"
        )

    failures = _failures(ordered)
    passed = not failures
    version = await _next_version(session, application_id)

    resume = Resume(
        id=uuid7(),
        profile_id=context.profile_id,
        application_id=application_id,
        kind="tailored",
        version=version,
        content=build_content(ordered),
        summary_text=" ".join(
            item.claim.claim_text for item in ordered if item.claim.section == "summary"
        )
        or None,
        verification_status=PASSED if passed else FAILED,
        verification_report=build_verification_report(ordered),
        agent_run_id=agent_run_id,
        model=model,
        prompt_version=prompt_version,
    )
    session.add(resume)
    await session.flush()

    # 3. One row per claim, copied from the handoff identity - never a second
    #    identity scheme, and never a re-derived verdict.
    for item in ordered:
        session.add(
            ResumeClaim(
                id=uuid7(),
                resume_id=resume.id,
                section=item.claim.section,
                path=item.claim.path,
                claim_text=item.claim.claim_text,
                fact_ids=[UUID(fid) for fid in item.claim.fact_ids],
                numerals_used=list(item.claim.numerals_used),
                verdict=item.verdict,
                verdict_reason=item.explanation,
                verified_at=item.verified_at,
            )
        )
    await session.flush()

    correlation = correlation_id or application_id
    emitted: list[str] = []

    # 4. The status move goes through the state machine, which writes the
    #    history row and `application.status.changed` itself.
    target = (
        ApplicationStatus.RESUME_READY
        if passed
        else ApplicationStatus.VERIFICATION_FAILED
    )
    await transition_application(
        session,
        application,
        target,
        actor_type="agent",
        actor_id=actor_id,
        reason=None if passed else f"{len(failures)} claim(s) failed verification",
        correlation_id=correlation,
        causation_id=causation_id,
        trace_id=trace_id,
    )
    emitted.append("application.status.changed")

    common: dict[str, Any] = {
        "aggregate_type": "application",
        "aggregate_id": application_id,
        "correlation_id": correlation,
        "causation_id": causation_id,
        "trace_id": trace_id,
        "actor_type": "agent",
        "actor_id": actor_id,
    }

    if passed:
        await emit(
            session,
            type="resume.generated",
            payload={
                "resume_id": str(resume.id),
                "application_id": str(application_id),
                "version": version,
                "claim_count": len(ordered),
                "verified_count": len(ordered) - len(failures),
                "agent_run_id": str(agent_run_id) if agent_run_id else None,
                "cost_usd": float(cost_usd) if cost_usd is not None else None,
            },
            **common,
        )
        emitted.append("resume.generated")
        # AGENT_DESIGN.md §3.4 B6: "emit `resume.generated` and request
        # rendering". Only on the success path - a failed resume is not
        # rendered, it is reviewed.
        if request_rendering:
            await emit(
                session,
                type="resume.render.requested",
                payload={"resume_id": str(resume.id), "formats": list(RENDER_FORMATS)},
                **common,
            )
            emitted.append("resume.render.requested")
    else:
        # The fabrication tripwire (EVENTS.md §3.2). No render request, and no
        # redraft: a human decides what happens next.
        await emit(
            session,
            type="resume.verification.failed",
            payload={
                "resume_id": str(resume.id),
                "application_id": str(application_id),
                "failures": failures,
            },
            **common,
        )
        emitted.append("resume.verification.failed")

    await session.flush()
    log.info(
        "resume.persisted",
        resume_id=str(resume.id),
        version=version,
        verification_status=resume.verification_status,
        claim_count=len(ordered),
        failed_claims=len(failures),
    )
    return PersistResult(
        resume_id=resume.id,
        version=version,
        verification_status=resume.verification_status,
        to_status=target,
        claim_count=len(ordered),
        verified_count=len(ordered) - len(failures),
        emitted=tuple(emitted),
    )


async def persist_resume_once(
    session: AsyncSession,
    *,
    triggering_event_id: UUID,
    consumer_group: str = CONSUMER_GROUP,
    **kwargs: Any,
) -> PersistResult | None:
    """Idempotent wrapper for event-driven callers.

    Delivery is at-least-once (EVENTS.md §4.1), so a redelivered
    `resume.generation.requested` must not produce a second resume version, a
    second set of claims, a second status history row or a second
    `resume.generated`.

    Identical to A5's `persist_evaluation_once`: claim `(event_id,
    consumer_group)` with `INSERT ... ON CONFLICT DO NOTHING` *before* any
    work, and decide from the insert itself. A SELECT-then-INSERT would let two
    concurrent deliveries both read "not processed" and both persist.

    The claim is written in the caller's transaction, so a failure downstream
    rolls it back too and a legitimate retry can re-claim. A genuinely new
    generation attempt arrives as a *different* triggering event and correctly
    produces the next append-only version.
    """
    from packages.core.events import claim_event, record_claim_result

    if not await claim_event(session, triggering_event_id, consumer_group):
        log.info(
            "resume.persist.skipped.already_claimed",
            event_id=str(triggering_event_id),
            consumer_group=consumer_group,
        )
        return None

    result = await persist_resume(session, **kwargs)
    await record_claim_result(
        session, triggering_event_id, consumer_group, result.verification_status
    )
    return result
