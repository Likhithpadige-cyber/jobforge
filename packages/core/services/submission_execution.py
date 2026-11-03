"""Human-authorized email submission execution (M11 Phase 2).

Connects an explicitly human-authorized `SUBMISSION_READY` application to
exactly one email submission attempt through the Phase 1 safety infrastructure.

Fundamental execution invariant:
  1. Explicit human Submit action (POST /applications/{id}/submit);
  2. Application is currently SUBMISSION_READY;
  3. Current application materials/approval state satisfies review completion;
  4. Current site policy is valid, unexpired, and explicitly selects EMAIL;
  5. Both SUBMISSION_MODE=enabled and persisted settings.submission_enabled=true;
  6. Email destination is grounded in trusted job data or confirmed matching domain;
  7. Submission attempt has not already been executed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from packages.core.events import emit
from packages.core.events.envelope import EventEnvelope
from packages.core.models import (
    Application,
    ApplicationStatus,
    Artifact,
    Event,
    Job,
    Profile,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
    User,
)
from packages.core.services.cover_letters import latest_cover_letter
from packages.core.services.handoff_packet import (
    NoCoverLetterError,
    ResumeNotRenderedError,
    generate_handoff_packet,
    load_packet_source,
    packet_storage_key,
)
from packages.core.services.resume_review import latest_tailored_resume
from packages.core.services.review_completion import review_is_complete
from packages.core.services.submission_gate import real_submission_enabled
from packages.core.services.submission_readiness import existing_submission, policy_verdict
from packages.core.state_machines.application import transition_application
from packages.services.delivery.email import (
    EmailAttachment,
    EmailSendStatus,
    OutboundEmail,
    build_email_transport,
)
from packages.services.ingestion.policy import PolicyDecision, extract_domain, extract_email
from packages.shared.clock import utc_now, utc_today
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.services.rendering.store import ArtifactStore
    from packages.shared.config import Settings

log = get_logger(__name__)


def extract_and_validate_authoritative_email(
    job: Job,
    site_policy: SitePolicy,
) -> str:
    """Extract and validate destination email solely from authoritative persisted job data.

    In Phase 2, the authoritative source is `job.url` containing an explicit `mailto:` URI.
    Validates that:
      1. `job.url` contains an explicit `mailto:` URI with a valid email.
      2. The email domain matches the site policy's target domain (or subdomain).

    Caller-supplied emails, domain guessing, scraping, or non-mailto URLs fail closed.
    """
    from_url = extract_email(job.url)
    if not from_url:
        raise ValidationError(
            f"Job {job.id} URL ({job.url}) is not an authoritative mailto: destination email"
        )

    dest_domain = extract_domain(from_url)
    policy_domain = getattr(site_policy, "domain", None)
    if isinstance(policy_domain, str):
        target_domain = policy_domain.lower()
    else:
        target_domain = str(getattr(site_policy, "target_domain", "")).lower()
    if dest_domain != target_domain and not dest_domain.endswith(f".{target_domain}"):
        raise ValidationError(
            f"Job mailto domain {dest_domain!r} does not match "
            f"target policy domain {target_domain!r}"
        )
    return from_url


async def validate_submit_preconditions(
    session: AsyncSession,
    application: Application,
    settings: Settings,
) -> tuple[Submission, str]:
    """Validate all preconditions before accepting a human Submit action.

    Revalidates:
      - application is SUBMISSION_READY
      - submission safety gate (both switches)
      - review completeness (all required materials approved)
      - policy allows EMAIL and review is not expired
      - attempt 1 submission row exists in PENDING state
      - destination email is authoritative and matches policy domain
    """
    if application.status is not ApplicationStatus.SUBMISSION_READY:
        raise ConflictError(
            f"Application {application.id} is not submission_ready "
            f"(currently {application.status})"
        )

    if not await real_submission_enabled(session, settings):
        raise ConflictError(
            "Real submission is disabled in system configuration or operational settings"
        )

    if not await review_is_complete(session, application):
        raise ConflictError(
            f"Application {application.id} materials do not satisfy all required approvals"
        )

    verdict, snapshot, policy_row, domain = await policy_verdict(session, application)
    if (
        verdict.decision is not PolicyDecision.ALLOWED
        or snapshot.submission_method is not SubmissionMethod.EMAIL
    ):
        raise ConflictError(
            f"Site policy for {domain} does not permit automated email submission "
            f"({verdict.reason or snapshot.submission_method})"
        )

    if (
        policy_row is not None
        and policy_row.next_review_due
        and policy_row.next_review_due < utc_today()
    ):
        raise ConflictError(
            f"Site policy review for {domain} expired on {policy_row.next_review_due}"
        )

    submission = await existing_submission(session, application.id)
    if submission is None:
        raise ConflictError(f"Application {application.id} has no attempt-1 submission record")

    if submission.result is not SubmissionResult.PENDING or submission.started_at is not None:
        raise ConflictError(
            f"Application {application.id} submission attempt has already been executed "
            f"(result={submission.result})"
        )

    job = await session.get(Job, application.job_id)
    if job is None:
        raise ConflictError(
            f"Application {application.id} references missing job {application.job_id}"
        )
    assert policy_row is not None
    dest = extract_and_validate_authoritative_email(job, policy_row)

    return submission, dest


async def request_submission(
    session: AsyncSession,
    application_id: UUID,
    settings: Settings,
    *,
    actor: User,
    causation_id: UUID | None = None,
) -> Submission:
    """Accept an explicit human Submit action, transition to SUBMITTING,
    and emit submission.requested.

    Destination is derived solely from authoritative persisted job data.
    Does NOT accept caller-supplied destination email.
    Does NOT set submission.started_at (started_at is set in worker when execution begins).
    """
    stmt = (
        select(Application)
        .join(Profile, Application.profile_id == Profile.id)
        .where(Application.id == application_id, Profile.user_id == actor.id)
        .with_for_update(of=Application)
        .execution_options(populate_existing=True)
    )
    application = (await session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"Application {application_id} not found")

    submission, _dest = await validate_submit_preconditions(session, application, settings)

    # Lock submission row as well
    await session.execute(
        select(Submission).where(Submission.id == submission.id).with_for_update()
    )

    # DO NOT set submission.started_at here. started_at means execution actually began.

    await transition_application(
        session,
        application,
        ApplicationStatus.SUBMITTING,
        actor_type="human",
        actor_id=str(actor.id),
        reason="human authorized email submission",
        correlation_id=application.id,
        causation_id=causation_id,
    )

    await emit(
        session,
        type="submission.requested",
        aggregate_type="submission",
        aggregate_id=submission.id,
        correlation_id=application.id,
        causation_id=causation_id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "submission_id": str(submission.id),
            "application_id": str(application.id),
            "requested_by": str(actor.id),
        },
    )

    return submission


async def execute_submission_attempt(
    session: AsyncSession,
    event: Event,
    *,
    settings: Settings,
    store: ArtifactStore,
) -> str:
    """Execute exactly one submission attempt from a submission.requested event.

    Runs within DeliveryRuntime on the dedicated 'submission' worker.
    """
    application_id = UUID(str(event.payload["application_id"]))

    stmt_app = (
        select(Application)
        .where(Application.id == application_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    application = (await session.execute(stmt_app)).scalar_one_or_none()
    if application is None:
        log.warning("submission_execution.application_missing", application_id=str(application_id))
        return "dead"

    stmt_sub = (
        select(Submission)
        .where(Submission.application_id == application_id, Submission.attempt == 1)
        .with_for_update()
    )
    submission = (await session.execute(stmt_sub)).scalar_one_or_none()
    if submission is None:
        log.warning("submission_execution.submission_missing", application_id=str(application_id))
        return "dead"

    # Idempotency: if already processed or not in SUBMITTING, safely skip
    if (
        application.status is not ApplicationStatus.SUBMITTING
        or submission.result is not SubmissionResult.PENDING
        or submission.finished_at is not None
    ):
        log.info(
            "submission_execution.already_processed",
            application_id=str(application_id),
            status=str(application.status),
            result=str(submission.result),
        )
        return "completed"
    return await _execute_attempt(
        session, application, submission, event, settings=settings, store=store
    )


async def _fail_preflight(
    session: AsyncSession,
    application: Application,
    submission: Submission,
    event: Event,
    reason: str,
) -> str:
    submission.result = SubmissionResult.FAILED
    submission.failure_reason = reason
    submission.started_at = None
    submission.finished_at = utc_now()
    submission.next_attempt_after = None
    await transition_application(
        session,
        application,
        ApplicationStatus.SUBMISSION_FAILED,
        actor_type="system",
        actor_id="submission_worker",
        reason=reason,
        correlation_id=application.id,
        causation_id=event.id,
    )
    await emit(
        session,
        type="submission.failed",
        aggregate_type="submission",
        aggregate_id=submission.id,
        correlation_id=application.id,
        causation_id=event.id,
        actor_type="system",
        actor_id="submission_worker",
        payload={
            "submission_id": str(submission.id),
            "application_id": str(application.id),
            "attempt": submission.attempt,
            "failure_reason": reason,
            "will_retry": False,
            "ambiguous": False,
        },
    )
    await session.flush()
    return "completed"


async def _execute_attempt(
    session: AsyncSession,
    application: Application,
    submission: Submission,
    event: Event,
    *,
    settings: Settings,
    store: ArtifactStore,
) -> str:
    """Execute preflights, transport send, and outcome classification for one attempt."""
    # Preflight 1: Safety gate at execution time - both switches must still be enabled
    if not await real_submission_enabled(session, settings):
        log.warning(
            "submission_execution.switches_disabled_at_execution",
            application_id=str(application.id),
        )
        return await _fail_preflight(
            session,
            application,
            submission,
            event,
            "submission switches disabled at execution time",
        )

    # Preflight 2: Policy currency and method at execution time
    verdict, snapshot, policy_row, domain = await policy_verdict(session, application)
    if (
        verdict.decision is not PolicyDecision.ALLOWED
        or snapshot.submission_method is not SubmissionMethod.EMAIL
        or policy_row is None
        or (policy_row.next_review_due and policy_row.next_review_due < utc_today())
    ):
        reason = str(verdict.reason or "policy review expired")
        log.warning(
            "submission_execution.policy_disallowed_at_execution",
            application_id=str(application.id),
            reason=reason,
        )
        return await _fail_preflight(
            session,
            application,
            submission,
            event,
            f"policy disallowed at execution time: {reason}",
        )
    submission.policy_id = policy_row.id

    # Preflight 3: Revalidate materials & reviews at execution time
    if not await review_is_complete(session, application):
        log.error("submission_execution.reviews_incomplete", application_id=str(application.id))
        return await _fail_preflight(
            session,
            application,
            submission,
            event,
            "application reviews not complete at execution time",
        )

    # Preflight 4: Authoritative destination email from persisted job data
    stmt_job = select(Job).options(selectinload(Job.company)).where(Job.id == application.job_id)
    job = (await session.execute(stmt_job)).scalar_one_or_none()
    if job is None:
        log.error("submission_execution.job_missing", job_id=str(application.job_id))
        return await _fail_preflight(
            session, application, submission, event, "job missing at execution time"
        )

    try:
        dest_email = extract_and_validate_authoritative_email(job, policy_row)
    except ValidationError as exc:
        log.error("submission_execution.destination_invalid", reason=str(exc))
        return await _fail_preflight(session, application, submission, event, str(exc))

    # Preflight 5: Artifacts and materials
    cover_letter = await latest_cover_letter(session, application.id)
    resume = await latest_tailored_resume(session, application.id)
    if cover_letter is None or resume is None:
        log.error("submission_execution.materials_missing", application_id=str(application.id))
        return await _fail_preflight(
            session, application, submission, event, "approved resume or cover letter missing"
        )

    resume_artifact = (
        await session.execute(
            select(Artifact).where(Artifact.resume_id == resume.id, Artifact.kind == "resume_pdf")
        )
    ).scalar_one_or_none()
    if resume_artifact is None:
        log.error("submission_execution.resume_artifact_missing", resume_id=str(resume.id))
        return await _fail_preflight(
            session, application, submission, event, "resume_pdf artifact not rendered"
        )

    try:
        pdf_bytes = await store.get(resume_artifact.storage_key)
    except Exception as exc:
        log.error("submission_execution.resume_artifact_read_failed", error=str(exc))
        return await _fail_preflight(
            session, application, submission, event, f"resume artifact could not be read: {exc}"
        )

    if not pdf_bytes:
        return await _fail_preflight(
            session, application, submission, event, "resume artifact is empty"
        )

    # Preflight 6: Packet artifact binding and currentness check
    if submission.packet_artifact_id is not None:
        packet_art = (
            await session.execute(
                select(Artifact).where(Artifact.id == submission.packet_artifact_id)
            )
        ).scalar_one_or_none()
        if packet_art is None:
            log.error(
                "submission_execution.packet_artifact_missing",
                artifact_id=str(submission.packet_artifact_id),
            )
            return await _fail_preflight(
                session, application, submission, event, "packet artifact missing at execution time"
            )

        # Content-addressed packet currentness check:
        # Confirm that packet_art.storage_key corresponds to the CURRENT approved materials
        try:
            source = await load_packet_source(session, application)
            expected_key = packet_storage_key(
                application_id=application.id,
                resume_id=source.resume.id,
                resume_version=source.resume.version,
                cover_letter_id=source.cover_letter.id,
                cover_letter_version=source.cover_letter.version,
                answers_digest=source.answers_digest,
            )
            if packet_art.storage_key != expected_key:
                log.error(
                    "submission_execution.packet_artifact_stale",
                    artifact_id=str(packet_art.id),
                    storage_key=packet_art.storage_key,
                    expected_key=expected_key,
                )
                return await _fail_preflight(
                    session,
                    application,
                    submission,
                    event,
                    "packet artifact is stale; does not match current approved materials",
                )
        except Exception as exc:
            log.error("submission_execution.packet_verification_failed", error=str(exc))
            return await _fail_preflight(
                session,
                application,
                submission,
                event,
                f"packet material verification failed: {exc}",
            )

    # Preflight 7: Prior attempts integrity checks (no prior success, no prior ambiguous)
    if submission.attempt > 1:
        stmt_priors = select(Submission).where(
            Submission.application_id == application.id,
            Submission.id != submission.id,
        )
        priors = (await session.execute(stmt_priors)).scalars().all()
        if any(p.result is SubmissionResult.SUCCEEDED for p in priors):
            log.error(
                "submission_execution.prior_success_exists", application_id=str(application.id)
            )
            return await _fail_preflight(
                session, application, submission, event, "prior attempt already succeeded"
            )
        if any(p.failure_reason and p.failure_reason.startswith("ambiguous:") for p in priors):
            log.error(
                "submission_execution.prior_ambiguous_exists", application_id=str(application.id)
            )
            return await _fail_preflight(
                session, application, submission, event, "prior attempt was ambiguous"
            )

    # Preflight 8: Attempt budget check
    if submission.attempt > settings.submission_max_attempts:
        log.error(
            "submission_execution.attempt_exceeds_budget",
            attempt=submission.attempt,
            max_attempts=settings.submission_max_attempts,
        )
        return await _fail_preflight(
            session,
            application,
            submission,
            event,
            (
                f"submission attempt {submission.attempt} exceeds "
                f"maximum attempts {settings.submission_max_attempts}"
            ),
        )

    # All preflights passed. Execution actually begins now!
    company_name = ""
    if job.company:
        company_name = getattr(job.company, "name", str(job.company))
    subject = f"Application for {job.title} - {company_name}".strip(" -")
    outbound = OutboundEmail(
        to=dest_email,
        subject=subject,
        body=cover_letter.body_text or "",
        attachments=(
            EmailAttachment(
                filename=f"Resume_{application.id}.pdf",
                content=pdf_bytes,
                content_type="application/pdf",
            ),
        ),
    )

    submission.started_at = utc_now()
    await emit(
        session,
        type="submission.started",
        aggregate_type="submission",
        aggregate_id=submission.id,
        correlation_id=application.id,
        causation_id=event.id,
        actor_type="system",
        actor_id="submission_worker",
        payload={
            "submission_id": str(submission.id),
            "application_id": str(application.id),
            "attempt": submission.attempt,
            "method": "email",
            "target_domain": submission.target_domain,
            "policy_id": str(submission.policy_id) if submission.policy_id else None,
        },
    )
    await session.flush()

    sender = settings.smtp_username or "applications@jobforge.invalid"
    transport = build_email_transport(settings, sender=sender)
    send_result = await transport.send(outbound)

    submission.finished_at = utc_now()
    submission.http_status = send_result.smtp_code
    submission.next_attempt_after = None

    if send_result.status is EmailSendStatus.SENT:
        submission.result = SubmissionResult.SUCCEEDED
        submission.external_reference = send_result.message_id
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMITTED,
            actor_type="system",
            actor_id="submission_worker",
            reason="email sent successfully",
            correlation_id=application.id,
            causation_id=event.id,
        )
        await emit(
            session,
            type="submission.succeeded",
            aggregate_type="submission",
            aggregate_id=submission.id,
            correlation_id=application.id,
            causation_id=event.id,
            actor_type="system",
            actor_id="submission_worker",
            payload={
                "submission_id": str(submission.id),
                "application_id": str(application.id),
                "method": "email",
                "external_reference": send_result.message_id,
                "confirmed_by": "system",
            },
        )
    elif send_result.status is EmailSendStatus.AMBIGUOUS:
        reason = f"ambiguous: {send_result.detail}"
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = reason
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="email send outcome ambiguous; human review required",
            correlation_id=application.id,
            causation_id=event.id,
        )
        await emit(
            session,
            type="submission.failed",
            aggregate_type="submission",
            aggregate_id=submission.id,
            correlation_id=application.id,
            causation_id=event.id,
            actor_type="system",
            actor_id="submission_worker",
            payload={
                "submission_id": str(submission.id),
                "application_id": str(application.id),
                "attempt": submission.attempt,
                "failure_reason": reason,
                "http_status": send_result.smtp_code,
                "will_retry": False,
                "ambiguous": True,
            },
        )
    elif send_result.status is EmailSendStatus.TRANSIENT_FAILURE:
        can_retry = submission.attempt < settings.submission_max_attempts
        if can_retry:
            delay_idx = submission.attempt - 1
            delay_sec = settings.submission_retry_delays_seconds[delay_idx]
            next_after = utc_now() + timedelta(seconds=delay_sec)
            submission.result = SubmissionResult.FAILED
            submission.failure_reason = send_result.detail
            submission.next_attempt_after = next_after
            await transition_application(
                session,
                application,
                ApplicationStatus.SUBMISSION_FAILED,
                actor_type="system",
                actor_id="submission_worker",
                reason=f"transient failure: {send_result.detail}; retry scheduled",
                correlation_id=application.id,
                causation_id=event.id,
            )
            await emit(
                session,
                type="submission.failed",
                aggregate_type="submission",
                aggregate_id=submission.id,
                correlation_id=application.id,
                causation_id=event.id,
                actor_type="system",
                actor_id="submission_worker",
                payload={
                    "submission_id": str(submission.id),
                    "application_id": str(application.id),
                    "attempt": submission.attempt,
                    "failure_reason": send_result.detail,
                    "http_status": send_result.smtp_code,
                    "will_retry": True,
                    "ambiguous": False,
                    "next_attempt_after": next_after.isoformat(),
                },
            )
        else:
            reason = f"exhausted {settings.submission_max_attempts} attempts: {send_result.detail}"
            submission.result = SubmissionResult.FAILED
            submission.failure_reason = reason
            submission.next_attempt_after = None
            await transition_application(
                session,
                application,
                ApplicationStatus.SUBMISSION_FAILED,
                actor_type="system",
                actor_id="submission_worker",
                reason=(
                    f"retry exhausted ({settings.submission_max_attempts} attempts): "
                    f"{send_result.detail}"
                ),
                correlation_id=application.id,
                causation_id=event.id,
            )
            await emit(
                session,
                type="submission.failed",
                aggregate_type="submission",
                aggregate_id=submission.id,
                correlation_id=application.id,
                causation_id=event.id,
                actor_type="system",
                actor_id="submission_worker",
                payload={
                    "submission_id": str(submission.id),
                    "application_id": str(application.id),
                    "attempt": submission.attempt,
                    "failure_reason": reason,
                    "http_status": send_result.smtp_code,
                    "will_retry": False,
                    "ambiguous": False,
                },
            )
    else:
        # REJECTED, AUTH_FAILED
        reason = f"email send failed: {send_result.status.value}"
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = send_result.detail
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason=reason,
            correlation_id=application.id,
            causation_id=event.id,
        )
        await emit(
            session,
            type="submission.failed",
            aggregate_type="submission",
            aggregate_id=submission.id,
            correlation_id=application.id,
            causation_id=event.id,
            actor_type="system",
            actor_id="submission_worker",
            payload={
                "submission_id": str(submission.id),
                "application_id": str(application.id),
                "attempt": submission.attempt,
                "failure_reason": send_result.detail,
                "http_status": send_result.smtp_code,
                "will_retry": False,
                "ambiguous": False,
            },
        )

    await session.flush()
    return "completed"


async def execute_submission_retry(
    session: AsyncSession,
    event: Event,
    *,
    settings: Settings,
    store: ArtifactStore,
) -> str:
    """Claim and execute an automatic retry attempt from submission.retry_requested.

    Runs within DeliveryRuntime on the dedicated 'submission' worker under app_agent.
    Atomically:
      1. Locks application and due failed submission;
      2. Revalidates retry eligibility;
      3. Clears next_attempt_after on the prior attempt;
      4. Transitions application SUBMISSION_FAILED -> SUBMITTING (system actor);
      5. Creates attempt N+1 append-only;
      6. Executes delivery preflights & transport via _execute_attempt.
    """
    application_id = UUID(str(event.payload["application_id"]))
    due_submission_id = UUID(str(event.payload["submission_id"]))

    stmt_app = (
        select(Application)
        .where(Application.id == application_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    application = (await session.execute(stmt_app)).scalar_one_or_none()
    if application is None:
        log.warning("submission_retry.application_missing", application_id=str(application_id))
        return "dead"

    stmt_sub = select(Submission).where(Submission.id == due_submission_id).with_for_update()
    prior_submission = (await session.execute(stmt_sub)).scalar_one_or_none()
    if prior_submission is None:
        log.warning("submission_retry.submission_missing", submission_id=str(due_submission_id))
        return "dead"

    # 1. Check if already claimed / not scheduled
    if prior_submission.next_attempt_after is None:
        log.info(
            "submission_retry.already_claimed",
            submission_id=str(due_submission_id),
            application_id=str(application_id),
        )
        return "completed"

    # 2. Check if not due yet
    now = utc_now()
    if prior_submission.next_attempt_after > now:
        log.info(
            "submission_retry.not_due_yet",
            submission_id=str(due_submission_id),
            due_at=prior_submission.next_attempt_after.isoformat(),
        )
        return "deferred"

    # 3. Check application status is SUBMISSION_FAILED
    if application.status is not ApplicationStatus.SUBMISSION_FAILED:
        log.warning(
            "submission_retry.invalid_application_status",
            application_id=str(application_id),
            status=str(application.status),
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 4. Check retry budget
    if prior_submission.attempt >= settings.submission_max_attempts:
        log.warning(
            "submission_retry.budget_exhausted",
            attempt=prior_submission.attempt,
            max_attempts=settings.submission_max_attempts,
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 5. Check no pending attempt already exists
    stmt_pending = select(Submission).where(
        Submission.application_id == application_id,
        Submission.result == SubmissionResult.PENDING,
    )
    pending_sub = (await session.execute(stmt_pending)).scalar_one_or_none()
    if pending_sub is not None:
        log.warning(
            "submission_retry.pending_attempt_already_exists",
            application_id=str(application_id),
            pending_id=str(pending_sub.id),
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 6. Check prior outcomes: never retry if prior attempt succeeded or was ambiguous
    stmt_all = select(Submission).where(Submission.application_id == application_id)
    all_subs = (await session.execute(stmt_all)).scalars().all()
    if any(s.result is SubmissionResult.SUCCEEDED for s in all_subs):
        log.warning(
            "submission_retry.prior_success_detected",
            application_id=str(application_id),
        )
        prior_submission.next_attempt_after = None
        return "completed"
    if any(s.failure_reason and s.failure_reason.startswith("ambiguous:") for s in all_subs):
        log.warning(
            "submission_retry.prior_ambiguous_detected",
            application_id=str(application_id),
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 7. Re-evaluate current submission policy
    verdict, snapshot, policy_row, domain = await policy_verdict(session, application)
    today = utc_today()
    is_expired = bool(
        policy_row and policy_row.next_review_due and policy_row.next_review_due < today
    )
    if (
        verdict.decision is not PolicyDecision.ALLOWED
        or snapshot.submission_method is not SubmissionMethod.EMAIL
        or policy_row is None
        or is_expired
    ):
        log.warning(
            "submission_retry.policy_invalid_at_claim",
            application_id=str(application_id),
            decision=str(verdict.decision),
            method=str(snapshot.submission_method) if snapshot else None,
            expired=is_expired,
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 8. Re-evaluate destination email from authoritative current job data
    stmt_job = select(Job).options(selectinload(Job.company)).where(Job.id == application.job_id)
    job = (await session.execute(stmt_job)).scalar_one_or_none()
    if job is None:
        log.warning("submission_retry.job_missing_at_claim", job_id=str(application.job_id))
        prior_submission.next_attempt_after = None
        return "completed"

    try:
        _dest_email = extract_and_validate_authoritative_email(job, policy_row)
    except ValidationError as exc:
        log.warning("submission_retry.destination_invalid_at_claim", reason=str(exc))
        prior_submission.next_attempt_after = None
        return "completed"

    # 9. Re-evaluate review completion
    if not await review_is_complete(session, application):
        log.warning(
            "submission_retry.reviews_incomplete_at_claim",
            application_id=str(application_id),
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # 10. Resolve current valid handoff packet matching current materials
    try:
        packet_artifact = await generate_handoff_packet(
            session,
            application=application,
            store=store,
            method=str(snapshot.submission_method),
            blocked_reason=None,
        )
    except (ResumeNotRenderedError, NoCoverLetterError, ConflictError, Exception) as exc:
        log.warning(
            "submission_retry.packet_resolution_failed_at_claim",
            application_id=str(application_id),
            error=str(exc),
        )
        prior_submission.next_attempt_after = None
        return "completed"

    # Eligibility confirmed. Clear next_attempt_after on attempt N atomically.
    prior_submission.next_attempt_after = None

    # Transition application SUBMISSION_FAILED -> SUBMITTING (system actor only)
    await transition_application(
        session,
        application,
        ApplicationStatus.SUBMITTING,
        actor_type="system",
        actor_id="submission_worker",
        reason=f"initiating retry attempt {prior_submission.attempt + 1}",
        correlation_id=application.id,
        causation_id=event.id,
    )

    # Create attempt N+1 append-only with current provenance
    new_submission = Submission(
        id=uuid7(),
        application_id=application.id,
        attempt=prior_submission.attempt + 1,
        method=snapshot.submission_method,
        target_domain=domain,
        policy_id=policy_row.id,
        result=SubmissionResult.PENDING,
        packet_artifact_id=packet_artifact.id,
        started_at=None,
        finished_at=None,
        next_attempt_after=None,
    )
    session.add(new_submission)
    await session.flush()

    # Re-execute preflights and delivery attempt for attempt N+1
    return await _execute_attempt(
        session, application, new_submission, event, settings=settings, store=store
    )


async def schedule_due_retries(
    session: AsyncSession,
    *,
    max_retries_per_tick: int = 50,
) -> int:
    """Discover due submission retries and emit submission.retry_requested.

    Runs within the periodic scheduler under the app_monitor role.
    Role constraints: SELECT on all tables, INSERT ON events.
    Strictly zero mutations on submissions or applications.
    """
    now = utc_now()
    stmt = (
        select(
            Submission.id,
            Submission.application_id,
            Submission.attempt,
            Submission.next_attempt_after,
        )
        .where(
            Submission.next_attempt_after.is_not(None),
            Submission.next_attempt_after <= now,
        )
        .order_by(Submission.next_attempt_after.asc())
        .limit(max_retries_per_tick)
    )
    due_rows = (await session.execute(stmt)).all()
    emitted = 0
    for sub_id, app_id, attempt, next_after in due_rows:
        if next_after is None:
            continue
        tick_id = uuid5(
            NAMESPACE_URL,
            f"jobforge:submission-retry:{sub_id}:{next_after.isoformat()}",
        )
        envelope = EventEnvelope(
            id=tick_id,
            type="submission.retry_requested",
            aggregate_type="submission",
            aggregate_id=sub_id,
            actor_type="system",
            actor_id="scheduler",
            correlation_id=app_id,
            occurred_at=now,
            payload={
                "submission_id": str(sub_id),
                "application_id": str(app_id),
                "attempt": attempt,
            },
        )
        stmt_ins = (
            pg_insert(cast(Table, Event.__table__))
            .values(**envelope.as_row())
            .on_conflict_do_nothing(index_elements=["id"])
        )
        result = await session.execute(stmt_ins)
        if result.rowcount > 0:
            emitted += 1
            log.info(
                "submission_retry.scheduled",
                submission_id=str(sub_id),
                application_id=str(app_id),
                attempt=attempt,
            )
    return emitted
