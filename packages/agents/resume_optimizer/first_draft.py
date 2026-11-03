"""M7 first-generation pipeline. No revision guidance, approvals or rendering."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.llm.runtime import CostLimitError, Request, Response, StructuredClient
from packages.agents.resume_optimizer import drafting, selection, summary
from packages.agents.resume_optimizer.handoff import (
    bullet_refs,
    sentence_refs,
    verify_claim_for_handoff,
)
from packages.agents.resume_optimizer.resume_persistence import persist_resume
from packages.core.db.inference_audit import FirstDraftInferenceAudit
from packages.core.generation_safety import require_generation_enabled
from packages.core.models import Application, ApplicationStatus, Event, Resume, User
from packages.core.services.first_draft import fingerprint, load_inputs
from packages.core.state_machines.application import transition_application
from packages.shared.errors import ConflictError, ForbiddenError, NotFoundError


class FirstDraftClient(StructuredClient):
    """The existing configured run budget applies across the entire first draft."""

    total_spent: Decimal = Decimal("0")

    async def complete(
        self,
        request: Request,
        *,
        spent: Decimal = Decimal("0"),
        session: AsyncSession | None = None,
    ) -> Response:
        response = await super().complete(
            request,
            spent=max(spent, self.total_spent),
            session=session,
        )
        self.total_spent += response.cost_usd
        # The stage must record actual usage before handling an over-budget result.
        return response


async def generate_first_draft(session: AsyncSession, event: Event,
                               client: FirstDraftClient) -> str:
    await require_generation_enabled(session)
    application = await session.scalar(select(Application).where(
        Application.id == event.aggregate_id,
    ).with_for_update())
    if application is None or event.aggregate_type != "application":
        raise NotFoundError("candidate or evidence not found")
    actor = await session.get(User, UUID(event.actor_id)) if event.actor_id else None
    if actor is None or actor.role != "owner" or event.actor_type != "human":
        raise ForbiddenError("first drafts require an owner request")
    if application.status != ApplicationStatus.CREATED:
        existing = await session.scalar(select(Resume).where(
            Resume.application_id == application.id, Resume.kind == "tailored",
        ).order_by(Resume.version).limit(1))
        if (existing is not None and existing.profile_id == application.profile_id
                and existing.diff_vs_base.get("first_draft_event_id") == str(event.id)):
            return f"completed:{existing.id}"
        raise ConflictError("first draft is no longer available; use review/rework")
    profile_id = UUID(event.payload["profile_id"])
    job_id = UUID(event.payload["job_id"])
    evaluation_id = UUID(event.payload["evaluation_id"])
    if (application.profile_id != profile_id or application.job_id != job_id
            or application.evaluation_id != evaluation_id):
        raise NotFoundError("candidate or evidence not found")
    context = await load_inputs(
        session, profile_id=profile_id, job_id=job_id,
        master_id=UUID(event.payload["master_resume_id"]), evaluation_id=evaluation_id,
    )
    if fingerprint(context) != event.payload["input_digest"]:
        raise ConflictError("tailoring inputs changed after the owner request")
    application_id = application.id
    writer = FirstDraftInferenceAudit(event_id=event.id, application_id=application.id,
                                       profile_id=profile_id)
    await transition_application(session, application, ApplicationStatus.RESUME_GENERATING,
                                 actor_type="agent", actor_id="resume_optimizer",
                                 correlation_id=event.correlation_id, causation_id=event.id)
    selected = await selection.select_content(
        session, context=context, client=client, writer=writer,
    )
    bullets = await drafting.draft_content(session, context=context, selection=selected,
                                            client=client, writer=writer)
    sentences = await summary.draft_summary(session, context=context, selection=selected,
                                            client=client, writer=writer)
    refs = bullet_refs(bullets.bullets, selected) + sentence_refs(sentences.sentences)
    if not refs:
        raise ConflictError("no grounded claims were generated")
    verified = tuple([
        await verify_claim_for_handoff(
            session, claim=claim, context=context, aggregate_id=application.job_id,
            client=client, writer=writer, claim_index=index,
        ) for index, claim in enumerate(refs)
    ])
    if client.total_spent > client.max_cost:
        raise CostLimitError("first-draft run budget exceeded")
    # Revalidate the full snapshot after inference as well as before it.
    session.expire_all()
    await session.refresh(event)
    fresh = await load_inputs(session, profile_id=profile_id, job_id=job_id,
                              master_id=context.base_resume_id, evaluation_id=evaluation_id)
    if fingerprint(fresh) != event.payload["input_digest"]:
        raise ConflictError("tailoring evidence changed during generation")
    result = await persist_resume(
        session, application_id=application_id, verified=verified,
        bullets=bullets.bullets, sentences=sentences.sentences, context=context,
        selection=selected, correlation_id=event.correlation_id, causation_id=event.id,
        model=client.model, cost_usd=client.total_spent, request_rendering=False,
        prompt_version=f"{selected.prompt_version}/{bullets.prompt_version}",
    )
    resume = await session.get(Resume, result.resume_id)
    if resume is None:
        raise RuntimeError("persisted first draft is missing")
    resume.diff_vs_base = {
        **resume.diff_vs_base, "first_draft_event_id": str(event.id),
        "profile_id": str(profile_id), "master_resume_id": str(context.base_resume_id),
        "master_resume_version": context.base_resume_version, "evaluation_id": str(evaluation_id),
        "profile_version": context.profile_version, "input_digest": event.payload["input_digest"],
        "master_fact_ids": [str(f.id) for f in context.facts],
        "gaps": list(context.gaps), "requires_human_review": True,
    }
    await session.flush()
    return f"{result.verification_status}:{resume.id}"
