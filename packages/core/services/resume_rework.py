"""Atomic API orchestration of a human decision and C3 regeneration.

**This module owns transaction boundaries, and it is the only service here
that does.** Everywhere else in this codebase repositories and services never
commit - the route's session dependency does. That convention assumes one unit
of work per request, and a durable revision attempt is not one unit of work:
the whole point (PO-4) is that the human decision and the attempt record
survive a generation failure that happens minutes later. So the boundaries are
written out here, in one place, rather than hidden behind a helper.

    T1   human decision + durable attempt row            COMMIT
    T2   acquire/fence the attempt + resume_generating   COMMIT
    T3   generate, verify, persist the domain result     COMMIT or ROLLBACK
    T4   mark the attempt succeeded                      COMMIT
    T4'  mark the attempt failed (on any exception)      COMMIT
    T5   recover the application when allowed            COMMIT

What that buys, and what it costs. Before, a pipeline exception rolled back
the decision, the status move, the history row and the events - the system
kept no evidence the attempt happened, which is untenable once an attempt can
cost real money. Now the evidence survives and the application is returned to
`changes_requested` by an explicit recorded transition (T5) rather than by the
absence of one. The end state a reviewer sees is the same; the audit trail is
not.

**Partial delivery, deliberately.** This batch makes the *attempt* durable. It
does not introduce `AuditBoundary` or the `app_audit` session, so
`agent_runs` / `agent_run_steps` / `llm_calls` rows still ride the domain
transaction and still roll back with a failed T3. The attempt row is not the
whole audit solution - it is the part that records that an attempt existed,
what it cost the user in time, and why it stopped.
"""

from __future__ import annotations

import asyncio
import hashlib
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

from packages.agents.resume_optimizer.revision import (
    RevisionRequest,
    load_revision_request,
)
from packages.core.db.audit_boundary import AuditBoundary, AuditContext
from packages.core.models import (
    ApplicationStatus,
    ApprovalStatus,
    RevisionAttemptStatus,
    RevisionFailureClass,
    RevisionOutcome,
)
from packages.core.repositories.revision_attempts import RevisionAttemptRepository
from packages.core.services import resume_review, resume_revision
from packages.shared.config import get_settings
from packages.shared.errors import AppError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.agents.llm.runtime import StructuredClient
    from packages.core.models import ResumeRevisionAttempt, User
    from packages.shared.config import Settings

log = get_logger(__name__)

#: Stateless, so constructing one per call site costs nothing and no instance
#: outlives the transaction it reports on.
audit = AuditBoundary()


def instructions_digest(instructions: str) -> str:
    """Bind the attempt to the exact note it was created for.

    Compared on re-entry: a replay carrying different instructions is a
    different revision, not a retry of this one.
    """
    return hashlib.sha256(instructions.encode()).hexdigest()


def classify(exc: BaseException) -> tuple[RevisionFailureClass, str]:
    """Map a pipeline exception to a technical failure class (G).

    Deliberately coarse and deliberately free of model prose: `failure_detail`
    records the exception type and a fixed message, never the model's output
    or the reviewer's text, both of which may carry private data into a column
    that operators read casually.

    A B5 semantic negative never arrives here. It is not an exception - it is
    a verdict, and it produces a SUCCEEDED attempt with
    `outcome='failed_verification'`.
    """
    from packages.agents.resume_optimizer.revision import RevisionNotAvailableError

    name = type(exc).__name__
    mapping: dict[str, RevisionFailureClass] = {
        "ReplayMissError": RevisionFailureClass.MODEL_OUTPUT_INVALID,
        "ModelOutputError": RevisionFailureClass.MODEL_OUTPUT_INVALID,
        "CostLimitError": RevisionFailureClass.BUDGET_EXCEEDED,
        "TimeoutError": RevisionFailureClass.MODEL_TIMEOUT,
        "ProviderError": RevisionFailureClass.MODEL_TIMEOUT,
        "MalformedMatchMatrixError": RevisionFailureClass.EVIDENCE_STALE,
        "UngroundedSelectionError": RevisionFailureClass.EVIDENCE_STALE,
    }
    if isinstance(exc, RevisionNotAvailableError):
        return RevisionFailureClass.STATE_CONFLICT, f"{name}: revision not available"
    if name in mapping:
        return mapping[name], f"{name} during revision"
    if isinstance(exc, AppError):
        return RevisionFailureClass.PERSISTENCE_FAILED, f"{name} during revision"
    return RevisionFailureClass.PERSISTENCE_FAILED, f"unexpected {name} during revision"


async def request_changes_and_regenerate(
    session: AsyncSession,
    *,
    application_id: UUID,
    approval_id: UUID,
    actor: User,
    note: str,
    client: StructuredClient,
) -> resume_review.ReviewState:
    """Record the decision, then run the revision against a durable attempt."""
    instructions = note.strip()
    if not instructions:
        raise resume_review.MissingNoteError("requesting changes needs a note")

    attempt, request = await _t1_decide_and_record(
        session,
        application_id=application_id,
        approval_id=approval_id,
        actor=actor,
        instructions=instructions,
    )
    # Snapshot the identity now, while the instance is certainly live. Below
    # this line the attempt travels as a UUID, never as an ORM instance: a
    # `session.rollback()` expires every persistent instance unconditionally -
    # `expire_on_commit=False` governs commit only - so a post-rollback
    # `attempt.id` is a lazy refresh, which in async SQLAlchemy is a
    # `MissingGreenlet`, raised from inside the handler for the real failure.
    attempt_id = attempt.id
    # Same reasoning, same boundary, different object. `actor` is the
    # authenticated `User`, loaded on this very session by the auth dependency,
    # so it expires with everything else the moment T3 rolls back. Downstream
    # it is only ever `str(actor.id)` in audit metadata, so the id is the whole
    # of what crosses.
    actor_id = actor.id

    # --- everything below decides whether there is work, before any write ---

    # (a) The attempt already closed successfully. Nothing to do.
    if attempt.status is RevisionAttemptStatus.SUCCEEDED:
        log.info(
            "resume_rework.attempt_already_succeeded",
            application_id=str(application_id),
            attempt_id=str(attempt.id),
        )
        return await resume_review.review_state(session, application_id)

    # (b) The result exists but the attempt does not say so - the T3/T4 crash
    # window. Adopt the committed result instead of generating a second
    # version of the same request. `completed_revision` matches on the
    # revision fingerprint (application + prior resume + version + approval),
    # so it recognises this request's own work and nothing else's.
    completed = await resume_revision.completed_revision(
        session, application_id=application_id, request=request
    )
    if completed is not None:
        await _reconcile(
            session,
            attempt_id=attempt_id,
            application_id=application_id,
            approval_id=request.approval_id,
            resume_id=completed.resume_id,
            version=completed.version,
            verification_status=completed.verification_status,
        )
        return await resume_review.review_state(session, application_id)

    # (c) The attempt is terminally failed. `failed` is terminal by trigger and
    # `acquire()` will not touch it, so re-driving is not merely refused here -
    # it is impossible. Releasing and replacing it is an explicit human action
    # (T6, PO-3) and is not part of this batch.
    if attempt.status is RevisionAttemptStatus.FAILED:
        raise resume_review.ReviewStateError(
            f"this revision attempt failed terminally "
            f"({attempt.failure_class}) and cannot be retried automatically; "
            "it must be released and replaced by a reviewer"
        )

    return await _run_attempt(
        session,
        attempt_id=attempt_id,
        request=request,
        application_id=application_id,
        actor_id=actor_id,
        client=client,
    )


async def _reconcile(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    application_id: UUID,
    approval_id: UUID,
    resume_id: UUID,
    version: int,
    verification_status: str,
) -> None:
    """Adopt a committed result into an attempt that never recorded it.

    Its own transaction, and best-effort: if the attempt cannot be claimed -
    because another worker holds a live lease and is about to run its own T4 -
    the resume is still correct and still returned to the caller. Two writers
    must not both decide the outcome of one attempt.
    """
    outcome = (
        RevisionOutcome.PASSED
        if verification_status == "passed"
        else RevisionOutcome.FAILED_VERIFICATION
    )
    reconciled = await RevisionAttemptRepository(session).reconcile_succeeded(
        attempt_id,
        owner=f"reconcile-{uuid7()}",
        result_resume_id=resume_id,
        result_version=version,
        outcome=outcome,
    )
    await session.commit()
    log.info(
        "resume_rework.reconciled" if reconciled else "resume_rework.reconcile_skipped",
        application_id=str(application_id),
        attempt_id=str(attempt_id),
        resume_id=str(resume_id),
        version=version,
    )
    # Both outcomes are recorded, not just the skip. "We adopted a result the
    # attempt never claimed" and "we declined to, because someone else holds
    # the lease" are different stories about the same crash window, and an
    # operator reconstructing one needs to know which happened.
    await audit.try_record(
        "revision.attempt.reconciled" if reconciled else "revision.attempt.reconcile_skipped",
        context=AuditContext(
            attempt_id=attempt_id,
            application_id=application_id,
            approval_id=approval_id,
        ),
        detail={"resume_id": str(resume_id), "version": version, "outcome": outcome.value},
    )


async def _t1_decide_and_record(
    session: AsyncSession,
    *,
    application_id: UUID,
    approval_id: UUID,
    actor: User,
    instructions: str,
) -> tuple[ResumeRevisionAttempt, RevisionRequest]:
    """T1. The human decision and the attempt row, committed together.

    **Re-entry matters more than the first pass.** `request_changes` is
    idempotent, so a replay finds the decision already recorded and writes
    nothing - but that must not end the request. Once T1 has committed, an
    attempt exists and may be `accepted`, `running` with a dead lease, or
    `failed`; returning early on "already decided" would leave the workflow
    permanently stranded in whatever state the previous call died in. The
    decision is idempotent; the workflow after it is resumable.
    """
    await resume_review._load_application(session, application_id, lock=True)
    approval = await resume_review.resume_approval(session, application_id)
    if approval is None or approval.id != approval_id:
        raise resume_review.StaleResumeError("The review changed; reload before deciding")

    if approval.status is ApprovalStatus.CHANGES_REQUESTED:
        if approval.changes_requested != instructions or approval.decided_by != actor.id:
            raise resume_review.ReviewStateError("This review already has a different decision")
    else:
        await resume_review.request_changes(
            session, application_id=application_id, actor=actor, note=instructions
        )

    request = await load_revision_request(
        session, application_id, lock=True, require_changes_requested=False
    )
    attempts = RevisionAttemptRepository(session)
    attempt = await attempts.resolve_or_create(
        request,
        requested_by=actor.id,
        instructions_digest=instructions_digest(instructions),
    )
    await session.commit()
    log.info(
        "resume_rework.attempt_resolved",
        application_id=str(application_id),
        attempt_id=str(attempt.id),
        status=str(attempt.status),
        attempt_count=attempt.attempt_count,
    )
    return attempt, request


async def _run_attempt(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    request: RevisionRequest,
    application_id: UUID,
    actor_id: UUID,
    client: StructuredClient,
) -> resume_review.ReviewState:
    """T2 through T5.

    **No ORM instance crosses into here.** The attempt is a `UUID`, the actor
    is a `UUID`, and `request` is a frozen dataclass of primitives. That is
    deliberate rather than tidy: T2 can roll back while waiting for a lease
    and T3 rolls back on any failure, and a rollback expires every persistent
    instance in the session whatever `expire_on_commit` says. An ORM argument
    here is a lazy refresh waiting for the first attribute read - in async, a
    `MissingGreenlet` raised from inside the handler for the real error. The
    only instance this function touches is the `Application` that T5 loads
    itself, after the rollback that would have expired it.
    """
    settings = get_settings()
    attempts = RevisionAttemptRepository(session)
    owner = f"api-{uuid7()}"

    # --- T2: fence, then move the application into generation -----------
    # Two identical requests must produce one attempt, one generator and one
    # result - and both callers should receive that result. Before durable
    # attempts, the loser blocked on the application row lock for the whole
    # pipeline and then found the winner's work by fingerprint. Committing T1
    # releases that lock, so the wait is explicit now rather than emergent.
    # One context for every diagnostic below. Built after the lease so
    # `lease_epoch` is the real fence value, and frozen primitives so it
    # survives the rollbacks T3 and T5 are built around.
    epoch = await _acquire_or_wait(
        session,
        attempts=attempts,
        attempt_id=attempt_id,
        application_id=application_id,
        approval_id=request.approval_id,
        owner=owner,
        settings=settings,
    )
    if epoch is None:
        # The winner finished while we waited: adopt its result rather than
        # generating a second version of the same request.
        completed = await resume_revision.completed_revision(
            session, application_id=application_id, request=request
        )
        if completed is not None:
            return await resume_review.review_state(session, application_id)
        await session.rollback()
        raise resume_review.ReviewStateError(
            "a revision for this review is already running and did not finish "
            "within the wait window; reload the review and retry"
        )
    context = AuditContext(
        attempt_id=attempt_id,
        application_id=application_id,
        approval_id=request.approval_id,
        lease_epoch=epoch,
    )
    await resume_revision.begin_generation(
        session, application_id=application_id, request=request, actor_id=actor_id
    )
    await session.commit()

    # --- T3: the pipeline, then the domain result ------------------------
    try:
        result = await resume_revision.generate_revision(
            session,
            application_id=application_id,
            client=client,
            request=request,
        )
        await session.commit()
    except BaseException as exc:
        await session.rollback()
        if not await _t4_prime_record_failure(
            session, context=context, owner=owner, epoch=epoch, exc=exc
        ):
            # The whole point of PO-4 is that a failure leaves evidence. If
            # the fenced UPDATE matched no row, it did not - and continuing
            # quietly is how that goes unnoticed for a release. Not raised:
            # the original exception below is the cause, and replacing it
            # with a bookkeeping error would lose it.
            log.error(
                "resume_rework.failure_not_recorded",
                application_id=str(application_id),
                attempt_id=str(attempt_id),
                owner=owner,
                epoch=epoch,
                cause=type(exc).__name__,
            )
            await audit.try_record(
                "revision.attempt.failure_not_recorded",
                context=context,
                detail={"owner": owner, "cause": type(exc).__name__},
            )
        await _t5_recover(session, context=context, actor_id=actor_id)
        raise

    # --- T4: the attempt succeeded, whatever the verdict said ------------
    # A failed verification is a SUCCEEDED attempt: the job did exactly what
    # it should and v2 exists as a durable failed record. Recording it as a
    # technical failure would make the two axes indistinguishable, which is
    # the single thing this design most wants to avoid.
    outcome = (
        RevisionOutcome.PASSED
        if result.verification_status == "passed"
        else RevisionOutcome.FAILED_VERIFICATION
    )
    recorded = await attempts.succeed(
        attempt_id,
        owner=owner,
        epoch=epoch,
        result_resume_id=result.resume_id,
        result_version=result.version,
        outcome=outcome,
    )
    await session.commit()
    if not recorded:
        # Same invariant as the failure path, and the same reason it is logged
        # rather than raised: the resume is committed and correct, so the
        # caller must still get it. What is missing is the attempt's record of
        # having produced it, which leaves the row stranded in `running` for
        # the reaper.
        log.error(
            "resume_rework.success_not_recorded",
            application_id=str(application_id),
            attempt_id=str(attempt_id),
            owner=owner,
            epoch=epoch,
        )
        # The domain result is committed by now. Audit policy: past this
        # point an audit failure must never cause regeneration, so this is
        # `try_record` and its return value is deliberately ignored.
        await audit.try_record(
            "revision.attempt.success_not_recorded",
            context=context,
            detail={
                "owner": owner,
                "result_resume_id": str(result.resume_id),
                "result_version": result.version,
            },
        )
    return await resume_review.review_state(session, application_id)


async def _acquire_or_wait(
    session: AsyncSession,
    *,
    attempts: RevisionAttemptRepository,
    attempt_id: UUID,
    application_id: UUID,
    approval_id: UUID,
    owner: str,
    settings: Settings,
) -> int | None:
    """Take the lease, or wait for whoever has it. Never run a second generator.

    Returns the epoch the lease was taken at - the fence for every later
    statement - or None if the work completed while waiting, in which case the
    caller adopts that result. Each tick rolls back first, because a
    read transaction opened before the winner committed would keep showing a
    stale snapshot for ever and the wait would time out against work that had
    already finished.

    Acquisition is retried on every tick rather than only at the start, which
    means the normal takeover rules still apply: if the holder's lease expires
    mid-wait, this caller adopts the SAME attempt and continues it. Fencing is
    untouched - `acquire()` is the only way in, and it still refuses a live
    lease.
    """
    deadline = monotonic() + settings.revision_wait_timeout_seconds
    while True:
        # Match T1: application before attempt. Keep both locks until the
        # caller commits the lease and begin_generation together in T2.
        await resume_review._load_application(session, application_id, lock=True)
        epoch = await attempts.acquire(
            attempt_id,
            owner=owner,
            lease_seconds=settings.revision_lease_ttl_seconds,
            max_attempts=settings.revision_max_attempts,
        )
        if epoch is not None:
            return epoch

        await session.rollback()
        current = await attempts.get(attempt_id)
        if current is None:  # pragma: no cover - the row cannot vanish
            return None
        if current.status is RevisionAttemptStatus.SUCCEEDED:
            return None
        if current.status is RevisionAttemptStatus.FAILED:
            raise resume_review.ReviewStateError(
                f"this revision attempt failed terminally ({current.failure_class}) "
                "and cannot be retried automatically; it must be released and "
                "replaced by a reviewer"
            )
        if monotonic() >= deadline:
            log.warning(
                "resume_rework.wait_timed_out",
                application_id=str(application_id),
                attempt_id=str(attempt_id),
                waited_seconds=settings.revision_wait_timeout_seconds,
            )
            # `lease_epoch` is left None on purpose: this caller never
            # acquired, so it holds no epoch, and writing 0 would put a
            # plausible-looking lie in a column operators reason about.
            await audit.try_record(
                "revision.attempt.wait_timed_out",
                context=AuditContext(
                    attempt_id=attempt_id,
                    application_id=application_id,
                    approval_id=approval_id,
                ),
                detail={
                    "waited_seconds": settings.revision_wait_timeout_seconds,
                    "holder_status": str(current.status),
                },
            )
            return None
        await asyncio.sleep(settings.revision_wait_poll_seconds)


async def _t4_prime_record_failure(
    session: AsyncSession,
    *,
    context: AuditContext,
    owner: str,
    epoch: int,
    exc: BaseException,
) -> bool:
    """T4'. Durable failure metadata, in its own transaction.

    **Returns whether the failure actually landed.** The fenced UPDATE matches
    one row or none, and "none" has two very different meanings: a legitimate
    takeover (another worker now owns this attempt and will record its own
    outcome) or a broken fence (the epoch this caller holds is not the one it
    acquired at). Both leave no evidence, so both must be visible to the
    caller rather than absorbed here.

    Best-effort by construction: if this cannot be written the original
    exception still propagates, because losing the audit record is bad and
    replacing the cause of the failure with a second failure is worse.
    """
    settings = get_settings()
    # `epoch` is passed rather than read off `context.lease_epoch`, which is
    # `int | None`: T4' is unreachable without a held lease, and expressing
    # that as a required argument is stronger than narrowing an Optional with
    # a runtime assert that `python -O` would strip.
    attempt_id = context.attempt_id
    failure_class, detail = classify(exc)
    try:
        status = await RevisionAttemptRepository(session).fail(
            attempt_id,
            owner=owner,
            epoch=epoch,
            failure_class=failure_class,
            detail=detail,
            max_attempts=settings.revision_max_attempts,
            evidence_stale_budget=settings.revision_evidence_stale_retry_budget,
        )
        await session.commit()
    except Exception as bookkeeping_exc:
        await session.rollback()
        log.exception("resume_rework.failure_record_failed", attempt_id=str(attempt_id))
        # The audit row goes to a different database connection, so it can
        # still land when the domain one is the thing that broke - which is
        # the case this event exists for.
        await audit.try_record(
            "revision.attempt.failure_record_failed",
            context=context,
            detail={
                "owner": owner,
                "bookkeeping_error": type(bookkeeping_exc).__name__,
                "original_cause": type(exc).__name__,
            },
        )
        return False
    if status is None:
        log.warning(
            "resume_rework.fenced_out_before_failure",
            attempt_id=str(attempt_id),
            owner=owner,
            epoch=epoch,
        )
        await audit.try_record(
            "revision.attempt.fenced_out_before_failure",
            context=context,
            detail={"owner": owner, "original_cause": type(exc).__name__},
        )
        return False
    log.info(
        "resume_rework.attempt_failed",
        attempt_id=str(attempt_id),
        failure_class=failure_class.value,
        status=status,
    )
    return True


async def _t5_recover(
    session: AsyncSession,
    *,
    context: AuditContext,
    actor_id: UUID,
) -> None:
    """T5. Return the application to `changes_requested`.

    **Two concerns, deliberately separated.** Recovering the *application*
    and stamping `recovered_at` on a *terminal attempt* were one step here,
    and conflating them stranded the common case: a retryable failure clears
    the lease and leaves the attempt `accepted`, so nothing holds the
    application, yet no `recovered_at` may be written (Batch 1 defines that
    column as failed-only) and the application therefore never moved. The next
    request then died at `assert_revisable`, because `resume_generating` is not
    a state a revision may start from.

    So the application moves whenever the attempt has released it - terminal
    `failed`, or `accepted` with a recorded failure and no live lease - while
    `mark_recovered()` stays exactly as Batch 1 wrote it and is called only for
    `failed`. A `running` attempt releases nothing, which is what stops a live
    worker's application being recovered out from under it.

    Idempotent on both paths, by different guards and for different reasons.
    `failed`: `recovered_at IS NULL` matches once, as before. `accepted`: the
    transition is conditional on the application still being in
    `resume_generating`, read under `FOR UPDATE`, so a second pass sees
    `changes_requested` and writes nothing. That guard is what makes the
    retryable path safe to reach repeatedly without a counter.
    """
    attempts = RevisionAttemptRepository(session)
    attempt_id, application_id = context.attempt_id, context.application_id
    try:
        # Match T1/T2 and inspect eligibility only after acquisition is
        # excluded by the application lock. A pre-lock read can go stale.
        application = await resume_review._load_application(
            session, application_id, lock=True
        )
        state = await attempts.recovery_state(attempt_id)
        if state is None or not state[1]:
            await session.rollback()
            return
        status = state[0]
        if status == "failed" and not await attempts.mark_recovered(attempt_id):
            # Already recovered by an earlier pass. The application moved with
            # that stamp; moving it again is neither needed nor safe.
            await session.rollback()
            return
        if application.status is ApplicationStatus.RESUME_GENERATING:
            await resume_revision.recover_to_changes_requested(
                session,
                application=application,
                actor_id=actor_id,
                attempt_id=attempt_id,
            )
        await session.commit()
    except Exception:
        await session.rollback()
        # Nothing moved, and the attempt still says so: `failed` with
        # `recovered_at` unset, or `accepted` with an application left in
        # `resume_generating`. Both are exactly the reaper's predicate -
        # detectable, and re-runnable. Never auto-replaced (PO-3).
        log.exception("resume_rework.recovery_failed", attempt_id=str(attempt_id))
        # The one diagnostic that already cost a batch: a missing
        # state-machine edge made every recovery raise, and the only evidence
        # was this log line, which nothing queried and no test asserted on.
        await audit.try_record("revision.attempt.recovery_failed", context=context)
