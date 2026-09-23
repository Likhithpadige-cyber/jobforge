"""M10 Phase 2 - submission readiness, the policy fan-out, and the human
handoff packet.

**Two gates, one condition, checked once.** `packages.core.services.
review_completion` owns `awaiting_approval -> approved` and defines the
condition ("current RESUME approved, current COVER_LETTER approved, current
ANSWERS approved if any were prepared"). This module owns `approved ->
submission_ready` and beyond, and imports that module's checking functions
rather than keeping a second copy of them - see `review_completion`'s
docstring for the stabilization this fixed (approval order no longer matters
for either gate) and for why re-checking the condition here is legitimate
defense in depth rather than a second source of truth: by construction,
nothing can make an application `approved` without the condition already
having held at that exact moment, and nothing edits a resume, a cover
letter, or an answer once `awaiting_approval` has been left.

**The exact ordering, end to end:**

    final required approval granted (RESUME, COVER_LETTER, or ANSWERS -
        whichever completes the set, in whatever order a human decided them)
      -> review_completion.maybe_mark_application_approved
           (awaiting_approval -> approved, same transaction)
      -> approval.granted recorded (same transaction, same commit)
      -- transaction commits; OutboxRelay publishes afterward --
      -> cg.application worker receives approval.granted
      -> check_readiness_on_approval: application.status is already
         `approved` (committed atomically with the grant, so this worker,
         running strictly after, can never observe a transitional state)
      -> compute_readiness: approved -> submission_ready -> policy fan-out

**One deterministic worker step, reacting to `approval.granted`.** No new
worker kind: this runs inside the existing `cg.application` consumer
(`workers/runtime.py`'s `"application"` kind), the same way M9's
`prepare_application` does, because readiness needs no model call - it is a
snapshot comparison, a policy lookup already built and tested
(`packages.services.ingestion.policy.evaluate_submission`), and a handful of
deterministic writes. `evt.approvals` is the correctly-routed stream for
`approval.granted` (ARCHITECTURE.md's own stream table already lists
`cg.application` as an expected consumer of it).

**Exactly once, replay-safe.** Work only proceeds while `applications.status
== 'approved'`; the first thing that happens once the condition holds is
leaving that status via `approved -> submission_ready`. A concurrent or
replayed `approval.granted` therefore either finds the row still `approved`
(and, serialized by the `Application` row lock this module takes before
checking anything, safely becomes the one call that proceeds) or finds it
already moved on (and no-ops). No new idempotency table, because M9's
status-based fix already proved this pattern sufficient for exactly this
shape of problem.

**The packet race (M10 Phase 2 stabilization, part C).** `resume.render.
requested` fires the instant the RESUME approval is granted, but the render
worker that turns it into a `resume_pdf` artifact is asynchronous and may not
have finished by the time readiness runs. `compute_readiness` never lets a
missing artifact block or corrupt the readiness decision - `packet_artifact_
id` simply stays `NULL` - and this module also subscribes to the existing
`resume.rendered` event (already routed to `evt.resumes`, already listed as
an expected `cg.application` stream in ARCHITECTURE.md's own table, exactly
like `evt.approvals` was) to retry packet generation once the artifact
exists, with no new event vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from packages.core.events import emit
from packages.core.models import (
    Application,
    ApplicationStatus,
    Job,
    SitePolicy,
    Submission,
    SubmissionResult,
)
from packages.core.repositories import SitePolicyRepository
from packages.core.services.handoff_packet import (
    PACKET_ELIGIBLE_STATUSES,
    NoCoverLetterError,
    ResumeNotRenderedError,
)
from packages.core.services.handoff_packet import generate_handoff_packet as _generate_packet
from packages.core.services.review_completion import review_is_complete
from packages.core.state_machines.application import transition_application
from packages.services.ingestion.pipeline import snapshot_policy
from packages.services.ingestion.policy import (
    BlockReason,
    PolicyDecision,
    PolicySnapshot,
    PolicyVerdict,
    evaluate_submission,
    extract_domain,
)
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.models import Event
    from packages.services.rendering.store import ArtifactStore

log = get_logger(__name__)

#: Alias kept local so callers of this module do not also need to import
#: `review_completion` themselves for the one function they need from it.
_readiness_satisfied = review_is_complete


async def _load_application(
    session: AsyncSession, application_id: UUID, *, lock: bool
) -> Application | None:
    stmt = select(Application).where(Application.id == application_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _policy_verdict(
    session: AsyncSession, application: Application
) -> tuple[PolicyVerdict, PolicySnapshot, SitePolicy | None, str]:
    job = await session.get(Job, application.job_id)
    assert job is not None  # applications.job_id is NOT NULL, FK ON DELETE RESTRICT
    domain = extract_domain(job.url)
    policy_row = await SitePolicyRepository(session).get_by_domain(domain)
    snapshot = snapshot_policy(policy_row, domain)
    verdict = evaluate_submission(snapshot)
    return verdict, snapshot, policy_row, domain


async def _existing_submission(session: AsyncSession, application_id: UUID) -> Submission | None:
    return (
        await session.execute(
            select(Submission).where(
                Submission.application_id == application_id, Submission.attempt == 1
            )
        )
    ).scalar_one_or_none()


async def compute_readiness(
    session: AsyncSession,
    *,
    application: Application,
    store: ArtifactStore,
    causation_id: UUID | None = None,
) -> None:
    """`approved -> submission_ready -> {stay | manual_handoff | policy_blocked}`,
    the `submissions` row, the handoff packet, and `application.ready`.

    Caller must already hold the `Application` row lock and must already have
    verified `application.status is APPROVED` and `_readiness_satisfied`.
    Idempotent regardless: `_existing_submission` refuses to create a second
    attempt-1 row, matching the "at most one" the unique index
    `uq_submissions_application_id_attempt` already enforces at the database
    layer even if this guard were ever bypassed.
    """
    if await _existing_submission(session, application.id) is not None:
        log.info(
            "submission_readiness.already_computed", application_id=str(application.id)
        )
        return

    await transition_application(
        session, application, ApplicationStatus.SUBMISSION_READY,
        actor_type="agent", actor_id="job_application",
        reason="resume, cover letter, and answers package (if any) all approved",
        correlation_id=application.id, causation_id=causation_id,
    )

    verdict, snapshot, policy_row, domain = await _policy_verdict(session, application)
    blocked_reason: str | None = None
    if verdict.decision is PolicyDecision.BLOCKED:
        blocked_reason = (
            verdict.reason.value if isinstance(verdict.reason, BlockReason) else "blocked"
        )
        await transition_application(
            session, application, ApplicationStatus.POLICY_BLOCKED,
            actor_type="agent", actor_id="job_application",
            reason=f"submission policy blocks {domain}: {blocked_reason}",
            correlation_id=application.id, causation_id=causation_id,
        )
        await emit(
            session, type="policy.blocked", aggregate_type="submission",
            aggregate_id=application.id, correlation_id=application.id,
            causation_id=causation_id, actor_type="system",
            payload={
                "domain": domain, "action": "submit",
                "policy_id": str(policy_row.id) if policy_row is not None else None,
                "reason": blocked_reason,
            },
        )
    elif verdict.decision is PolicyDecision.REQUIRES_HUMAN:
        await transition_application(
            session, application, ApplicationStatus.MANUAL_HANDOFF,
            actor_type="agent", actor_id="job_application",
            reason=f"{domain} requires a human to submit",
            correlation_id=application.id, causation_id=causation_id,
        )
    # ALLOWED: no further transition. M10 implements no dispatcher for any
    # channel, so the application stays at submission_ready - see the module
    # and state-machine docstrings for why this is not treated as
    # "a human should act now".

    submission = Submission(
        id=uuid7(),
        application_id=application.id,
        attempt=1,
        method=snapshot.submission_method,
        target_domain=domain,
        policy_id=policy_row.id if policy_row is not None else None,
        result=SubmissionResult.PENDING,
        blocked_reason=blocked_reason,
    )
    session.add(submission)
    await session.flush()

    packet_artifact_id: UUID | None = None
    try:
        artifact = await _generate_packet(
            session, application=application, store=store,
            method=str(snapshot.submission_method), blocked_reason=blocked_reason,
        )
        packet_artifact_id = artifact.id
    except (ResumeNotRenderedError, NoCoverLetterError) as exc:
        # Best-effort: the readiness decision itself must never be blocked or
        # corrupted by the packet. `packet_artifact_id` stays NULL - never a
        # row pointing at bytes that do not exist - and `retry_packet_
        # generation` below, subscribed to the existing `resume.rendered`
        # event, is the deterministic path that fills it in once the real
        # race this can hit resolves (resume.render.requested fires the
        # instant the resume is approved, but the render worker consuming it
        # is asynchronous). `GET /packet` only ever serves what already
        # exists - it does not retry on read.
        log.warning(
            "submission_readiness.packet_deferred",
            application_id=str(application.id), error=type(exc).__name__,
        )
    if packet_artifact_id is not None:
        submission.packet_artifact_id = packet_artifact_id
        await session.flush()

    await emit(
        session, type="application.ready", aggregate_type="application",
        aggregate_id=application.id, correlation_id=application.id,
        causation_id=causation_id, actor_type="agent", actor_id="job_application",
        payload={
            "application_id": str(application.id),
            "submission_method": str(snapshot.submission_method),
            "target_domain": domain,
        },
    )
    log.info(
        "submission_readiness.computed",
        application_id=str(application.id),
        final_status=str(application.status),
        submission_method=str(snapshot.submission_method),
        blocked_reason=blocked_reason,
    )


async def check_readiness_on_approval(
    session: AsyncSession, event: Event, *, store: ArtifactStore
) -> str:
    """The `cg.application` worker handler for `approval.granted`.

    `store` is built once at worker startup and passed in, the same way
    `workers/runtime.py`'s `"render"` branch already builds its
    `ArtifactStore` once rather than per event.

    A partial approval set is normal and returns "incomplete" without
    writing anything. Reached from any of the three approval kinds this
    depends on - the handler does not care which one just fired, only
    whether all three now hold for the current versions.
    """
    application_id = UUID(str(event.payload["application_id"]))
    application = await _load_application(session, application_id, lock=True)
    if application is None:
        return "not-found"
    if application.status is not ApplicationStatus.APPROVED:
        # Already computed (moved past `approved`), or not eligible yet
        # (still awaiting_approval). Either way, nothing to do - and this is
        # exactly what makes a replayed or late-arriving `approval.granted`
        # safe: readiness can only ever run once per application, because
        # running it is what leaves this status.
        return "not-approved"
    if not await _readiness_satisfied(session, application):
        return "incomplete"

    await compute_readiness(
        session, application=application, store=store, causation_id=event.id
    )
    return "completed"


async def retry_packet_generation(
    session: AsyncSession, event: Event, *, store: ArtifactStore
) -> str:
    """The `cg.application` worker handler for `resume.rendered` - the
    deterministic retry path for the packet race documented in this
    module's docstring (part C of the M10 Phase 2 stabilization).

    `resume.rendered` fires far more often than a packet is actually
    waiting on it: every time a resume is rendered at all, including well
    before the application is anywhere near `submission_ready`. Almost
    every delivery is therefore a safe no-op, decided cheaply and in order:
    application not past `approved` yet -> nothing to retry; no `submissions`
    row yet -> readiness itself has not run yet, nothing to retry; a packet
    already recorded -> already generated, nothing to retry. Only when a
    submission exists with no packet does this attempt generation, and only
    for an application whose status is one `generate_handoff_packet` will
    actually accept.
    """
    application_id = UUID(str(event.payload["application_id"]))
    application = await _load_application(session, application_id, lock=True)
    if application is None:
        return "not-found"
    if application.status not in PACKET_ELIGIBLE_STATUSES:
        return "not-ready"

    submission = await _existing_submission(session, application_id)
    if submission is None or submission.packet_artifact_id is not None:
        return "nothing-to-retry"

    try:
        artifact = await _generate_packet(
            session, application=application, store=store,
            method=str(submission.method), blocked_reason=submission.blocked_reason,
        )
    except (ResumeNotRenderedError, NoCoverLetterError) as exc:
        # Still not ready - a different resume version's render, or the
        # cover letter genuinely does not exist. Stays retryable on the next
        # resume.rendered delivery.
        log.info(
            "submission_readiness.packet_retry_still_pending",
            application_id=str(application_id), error=type(exc).__name__,
        )
        return "still-pending"

    submission.packet_artifact_id = artifact.id
    await session.flush()
    log.info(
        "submission_readiness.packet_retry_succeeded",
        application_id=str(application_id), artifact_id=str(artifact.id),
    )
    return "completed"
