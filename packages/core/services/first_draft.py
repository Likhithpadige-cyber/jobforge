"""Owner-requested first drafts; candidate checks precede inference."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from pydantic import ValidationError as SchemaError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.llm.runtime import canonical
from packages.agents.resume_optimizer.context import (
    TailoringContext,
    load_tailoring_context,
    validate_match_matrix,
)
from packages.core.events import EventEnvelope, record_event
from packages.core.models import (
    Application,
    ApplicationStatus,
    Event,
    Job,
    JobEvaluation,
    JobStatus,
    Profile,
    ProfileFact,
    Resume,
    User,
)
from packages.core.services.master_resume import MasterResumeData, check_master
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError, ForbiddenError, NotFoundError


@dataclass(frozen=True)
class FirstDraftRequest:
    application_id: UUID
    event_id: UUID
    profile_id: UUID


def fingerprint(context: TailoringContext) -> str:
    return hashlib.sha256(
        canonical(
            {
                "profile_id": str(context.profile_id),
                "profile_version": context.profile_version,
                "base_id": str(context.base_resume_id),
                "base_version": context.base_resume_version,
                "base": context.base_resume_structure,
                "evaluation_id": str(context.evaluation_id),
                "job": {
                    key: context.job.get(key)
                    for key in (
                        "title",
                        "normalized_title",
                        "description_raw",
                        "requirements",
                        "seniority",
                        "location_country",
                        "location_state",
                        "is_remote",
                        "is_hybrid",
                    )
                },
                "job_id": str(context.job["id"]),
                "matrix": context.match_matrix,
                "facts": [
                    {
                        "id": str(f.id),
                        "text": f.text,
                        "structured": f.structured,
                        "status": str(f.status),
                        "type": str(f.fact_type),
                    }
                    for f in context.facts
                ],
            }
        ).encode()
    ).hexdigest()


async def load_inputs(
    session: AsyncSession, *, profile_id: UUID, job_id: UUID, master_id: UUID, evaluation_id: UUID
) -> TailoringContext:
    base = await session.get(Resume, master_id)
    evaluation = await session.get(JobEvaluation, evaluation_id)
    if (
        base is None
        or base.kind != "base"
        or base.application_id is not None
        or base.profile_id != profile_id
        or evaluation is None
        or evaluation.job_id != job_id
        or evaluation.match_matrix.get("profile_id") != str(profile_id)
    ):
        raise NotFoundError("candidate or evidence not found")
    context = await load_tailoring_context(session, profile_id=profile_id, job_id=job_id)
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise NotFoundError("candidate or evidence not found")
    if (
        context.base_resume_id != master_id
        or context.evaluation_id != evaluation_id
        or context.match_matrix.get("profile_id") != str(profile_id)
    ):
        raise NotFoundError("candidate or evidence not found")
    if context.profile_version != profile.version:
        raise ConflictError("evaluation must be refreshed for the current profile")
    try:
        master = MasterResumeData.model_validate_json(canonical(context.base_resume_structure))
    except SchemaError:
        raise ConflictError(
            "master resume must use the reviewed structured import contract"
        ) from None
    master_ids = await check_master(session, profile_id, master)
    verified_ids = {f.id for f in context.facts}
    if not master_ids or not master_ids <= verified_ids:
        raise ConflictError("master evidence must be linked to current verified facts")
    linked = list(
        (
            await session.scalars(
                select(ProfileFact).where(
                    ProfileFact.id.in_(master_ids),
                )
            )
        ).all()
    )
    today = utc_now().date()
    if any(
        (f.valid_from is not None and f.valid_from > today)
        or (f.valid_to is not None and f.valid_to < today)
        for f in linked
    ):
        raise ConflictError("master evidence contains facts outside their validity period")
    # Generation can select only master-linked evidence. The full candidate
    # boundary was checked above; never trim a foreign citation to make it valid.
    validate_match_matrix(context.match_matrix, master_ids)
    return replace(context, facts=tuple(f for f in context.facts if f.id in master_ids))


async def request_first_draft(
    session: AsyncSession,
    *,
    actor: User,
    profile_id: UUID,
    job_id: UUID,
    master_id: UUID,
    evaluation_id: UUID,
) -> FirstDraftRequest:
    if actor.role != "owner":
        raise ForbiddenError("owner role required")
    job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise NotFoundError("candidate or evidence not found")
    if job.status in {JobStatus.DUPLICATE, JobStatus.EXPIRED, JobStatus.APPLIED}:
        raise ConflictError("job is not eligible for a first draft")
    context = await load_inputs(
        session,
        profile_id=profile_id,
        job_id=job_id,
        master_id=master_id,
        evaluation_id=evaluation_id,
    )
    application = await session.scalar(
        select(Application)
        .where(
            Application.job_id == job_id,
            Application.status.not_in(
                [
                    ApplicationStatus.ABANDONED,
                    ApplicationStatus.ERROR,
                    ApplicationStatus.POLICY_BLOCKED,
                ]
            ),
        )
        .with_for_update()
    )
    if application is not None and application.profile_id != profile_id:
        raise NotFoundError("candidate or evidence not found")
    if application is None:
        application = Application(
            job_id=job_id,
            profile_id=profile_id,
            evaluation_id=evaluation_id,
            status=ApplicationStatus.CREATED,
        )
        session.add(application)
        await session.flush()
    digest = fingerprint(context)
    event_id = uuid5(application.id, f"first-draft:{digest}")
    existing = await session.get(Event, event_id)
    if existing is not None:
        return FirstDraftRequest(application.id, event_id, profile_id)
    if application.status != ApplicationStatus.CREATED:
        raise ConflictError("first draft already started; use the existing review/rework workflow")
    application.evaluation_id = evaluation_id
    await record_event(
        session,
        EventEnvelope(
            id=event_id,
            type="resume.first_draft.requested",
            aggregate_type="application",
            aggregate_id=application.id,
            correlation_id=application.id,
            actor_type="human",
            actor_id=str(actor.id),
            payload={
                "profile_id": str(profile_id),
                "job_id": str(job_id),
                "master_resume_id": str(master_id),
                "evaluation_id": str(evaluation_id),
                "input_digest": digest,
            },
        ),
    )
    return FirstDraftRequest(application.id, event_id, profile_id)
