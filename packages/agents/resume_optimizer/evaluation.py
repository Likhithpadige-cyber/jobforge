"""Automatic deterministic evaluation handler for the transactional M4 runtime."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.resume_optimizer.assessment import Alignment, align, assess
from packages.agents.resume_optimizer.evidence import fact_views
from packages.agents.resume_optimizer.persistence import Thresholds, persist_evaluation
from packages.agents.resume_optimizer.requirements import extract_requirements
from packages.core.models import Event, Job, JobStatus, Profile, User
from packages.core.repositories import FactRepository
from packages.shared.errors import ValidationError


def evaluation_handler(profile_id: UUID | None, thresholds: Thresholds) -> Callable[
    [AsyncSession, Event], Awaitable[str]
]:
    async def evaluate(session: AsyncSession, event: Event) -> str:
        # Lock the aggregate as well as M4's per-delivery lock: separate events
        # for the same job must not both evaluate a newly normalized row.
        job = await session.scalar(select(Job).where(
            Job.id == event.aggregate_id,
        ).with_for_update())
        if job is None or event.aggregate_type != "job":
            raise ValidationError("evaluation source does not identify a job")
        if job.status != JobStatus.NORMALIZED:
            return "ineligible_job_state"
        query = select(Profile).join(User).where(User.role == "owner")
        if profile_id is not None:
            query = query.where(Profile.id == profile_id)
        candidates = list((await session.scalars(query.limit(2))).all())
        if len(candidates) != 1:
            raise ValidationError("evaluation requires one explicitly selected owner profile")
        profile = candidates[0]
        version = profile.version
        as_of = event.occurred_at.date()
        citable = await FactRepository(session).list_citable(profile.id)
        facts = fact_views([fact for fact in citable
                            if (fact.valid_from is None or fact.valid_from <= as_of)
                            and (fact.valid_to is None or fact.valid_to >= as_of)])
        # Parse only the description. Titles/company metadata never become requirements.
        extraction = extract_requirements(job.description_raw)
        alignments = align(
            facts, seniority=str(job.seniority), title=job.normalized_title or job.title,
            target_roles=profile.target_roles, is_remote=job.is_remote,
            is_hybrid=job.is_hybrid, location_state=job.location_state,
            location_country=job.location_country, profile_country=profile.location_country,
        )
        if re.search(r"\b(visa|sponsor\w*|work authori\w*|citizenship|clearance)\b",
                     job.description_raw or "", re.I):
            alignments["location"] = Alignment(None, "authorization requires human review")
        assessment = assess(
            extraction.requirements, facts, alignments=alignments,
            today=event.occurred_at.date(), extraction_confident=not extraction.needs_llm_fallback,
        )
        # Mutations bump this version in their transaction. Reject a mixed read
        # if facts changed while loading them; a retry obtains a fresh snapshot.
        current_version = await session.scalar(
            select(Profile.version).where(Profile.id == profile.id)
        )
        if current_version != version:
            raise ValidationError("profile changed during evaluation")
        evidence = assessment.evidence()
        evidence["profile_id"] = str(profile.id)
        evidence["profile_version"] = version
        evidence["as_of"] = event.occurred_at.date().isoformat()
        result = await persist_evaluation(
            session, job_id=job.id, profile_version=version,
            breakdown=assessment.breakdown, match_matrix=evidence,
            assessment_status=assessment.status, insufficient_reasons=assessment.reasons,
            thresholds=thresholds, correlation_id=event.correlation_id,
            causation_id=event.id, trace_id=event.trace_id,
        )
        return f"{assessment.status}:{result.evaluation_id}"
    return evaluate
