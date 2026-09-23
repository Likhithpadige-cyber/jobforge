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

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from packages.core.events import emit
from packages.core.models import (
    Application,
    ApplicationStatus,
    Artifact,
    Job,
    Profile,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
    User,
)
from packages.core.services.cover_letters import latest_cover_letter
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
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.models import Event
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

    submission, _dest = await validate_submit_preconditions(
        session, application, settings
    )

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

    # Preflight 1: Safety gate at execution time - both switches must still be enabled
    if not await real_submission_enabled(session, settings):
        log.warning(
            "submission_execution.switches_disabled_at_execution",
            application_id=str(application_id),
        )
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = "submission switches disabled at execution time"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="submission switches disabled at execution time",
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
                "failure_reason": "submission switches disabled at execution time",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    # Preflight 2: Policy currency at execution time
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
            application_id=str(application_id),
            reason=reason,
        )
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = f"policy disallowed at execution time: {reason}"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason=f"policy disallowed at execution time: {reason}",
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
                "failure_reason": f"policy disallowed at execution time: {reason}",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    # Preflight 3: Revalidate materials & reviews at execution time
    if not await review_is_complete(session, application):
        log.error("submission_execution.reviews_incomplete", application_id=str(application_id))
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = "application reviews not complete at execution time"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="application reviews not complete at execution time",
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
                "failure_reason": "application reviews not complete at execution time",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    # Preflight 4: Authoritative destination email from persisted job data
    stmt_job = select(Job).options(selectinload(Job.company)).where(Job.id == application.job_id)
    job = (await session.execute(stmt_job)).scalar_one_or_none()
    if job is None:
        log.error("submission_execution.job_missing", job_id=str(application.job_id))
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = "job missing at execution time"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="job missing at execution time",
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
                "failure_reason": "job missing at execution time",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    try:
        dest_email = extract_and_validate_authoritative_email(job, policy_row)
    except ValidationError as exc:
        log.error("submission_execution.destination_invalid", reason=str(exc))
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = str(exc)
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason=str(exc),
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
                "failure_reason": str(exc),
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    # Preflight 5: Artifacts and materials
    cover_letter = await latest_cover_letter(session, application.id)
    resume = await latest_tailored_resume(session, application.id)
    if cover_letter is None or resume is None:
        log.error("submission_execution.materials_missing", application_id=str(application_id))
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = "approved resume or cover letter missing"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="approved resume or cover letter missing",
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
                "failure_reason": "approved resume or cover letter missing",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    resume_artifact = (
        await session.execute(
            select(Artifact).where(Artifact.resume_id == resume.id, Artifact.kind == "resume_pdf")
        )
    ).scalar_one_or_none()

    if resume_artifact is None:
        log.error("submission_execution.resume_artifact_missing", resume_id=str(resume.id))
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = "resume_pdf artifact not rendered"
        submission.finished_at = utc_now()
        submission.next_attempt_after = None
        await transition_application(
            session,
            application,
            ApplicationStatus.SUBMISSION_FAILED,
            actor_type="system",
            actor_id="submission_worker",
            reason="resume_pdf artifact not rendered",
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
                "failure_reason": "resume_pdf artifact not rendered",
                "will_retry": False,
                "ambiguous": False,
            },
        )
        return "completed"

    pdf_bytes = await store.get(resume_artifact.storage_key)

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

    # All preflights passed. Execution actually begins now!
    # Set started_at and emit submission.started immediately before transport attempt.
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
    else:
        # REJECTED, AUTH_FAILED, TRANSIENT_FAILURE
        # (Phase 2 treats transient as failed without retry)
        reason = f"email send failed: {send_result.status.value}"
        submission.result = SubmissionResult.FAILED
        submission.failure_reason = send_result.detail
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
