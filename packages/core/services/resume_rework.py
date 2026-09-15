"""Atomic API orchestration of a human decision and C3 regeneration.

The approval ID binds the command to the review on screen. Keep the application
lock until the caller commits, including on the retry path. No commit here:
a pipeline exception rolls back the decision, history, events and new version.
A previously committed C2 decision can also be resumed by this entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from packages.core.models import ApprovalStatus
from packages.core.services import resume_review, resume_revision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.agents.llm.runtime import StructuredClient
    from packages.core.models import User


async def request_changes_and_regenerate(
    session: AsyncSession,
    *,
    application_id: UUID,
    approval_id: UUID,
    actor: User,
    note: str,
    client: StructuredClient,
) -> resume_review.ReviewState:
    instructions = note.strip()
    if not instructions:
        raise resume_review.MissingNoteError("requesting changes needs a note")
    await resume_review._load_application(session, application_id, lock=True)
    approval = await resume_review.resume_approval(session, application_id)
    if approval is None or approval.id != approval_id:
        raise resume_review.StaleResumeError("The review changed; reload before deciding")
    if approval.status == ApprovalStatus.CHANGES_REQUESTED:
        if approval.changes_requested != instructions or approval.decided_by != actor.id:
            raise resume_review.ReviewStateError("This review already has a different decision")
    else:
        await resume_review.request_changes(
            session,
            application_id=application_id,
            actor=actor,
            note=instructions,
        )
    await resume_revision.regenerate_after_changes(
        session,
        application_id=application_id,
        actor=actor,
        client=client,
    )
    return await resume_review.review_state(session, application_id)
