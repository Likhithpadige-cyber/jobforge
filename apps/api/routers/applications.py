"""M9 - application preparation: extraction, review and the answers approval.

    POST  /api/v1/applications/{id}/prepare               owner only
    GET   /api/v1/applications/{id}/questions              owner + reviewer
    PATCH /api/v1/applications/{id}/answers/{answer_id}    owner + reviewer
    POST  /api/v1/applications/{id}/package/approve        owner + reviewer
    POST  /api/v1/applications/{id}/package/request-changes owner + reviewer

Mirrors `apps/api/routers/resume_review.py`'s shape: thin handlers that
establish the human from the session, delegate to
`packages.core.services.applications`, and commit once. `prepare` is the one
pipeline-triggering, cost-shaped action (owner only, like
`POST /jobs/{id}/first-draft`); everything else is review, shared with the
reviewer role exactly as M2 already treats resume review.

Both routes that take an identifier check authentication before any lookup
and answer a missing/inaccessible resource identically to a nonexistent one,
the same non-enumeration discipline `apps/api/routers/artifacts.py`
documents.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from apps.api.deps import (
    CsrfProtected,
    CurrentHumanActor,
    DbSession,
    authenticated_api,
    rate_limited,
    require_owner,
)
from apps.api.schemas.applications import (
    AnswerRead,
    ApprovalRead,
    EditAnswerRequest,
    PackageApproveRequest,
    PackageRequestChangesRequest,
    PrepareRequest,
    PrepareResponse,
    QuestionRead,
)
from packages.core.cache.rate_limit import Limit
from packages.core.models import Application
from packages.core.services.applications import (
    RawQuestion,
    approve_answers,
    edit_answer,
    existing_questions,
    request_application_questions,
    request_changes_answers,
)
from packages.shared.config import Settings
from packages.shared.errors import NotFoundError

router = APIRouter(
    prefix="/applications", tags=["applications"], dependencies=[Depends(authenticated_api)]
)

#: Deliberately identical for "no such application" and "not a readable
#: one" - a distinct message would let a caller probe which identifiers
#: exist, same discipline as artifacts.py's `_MISSING`.
_MISSING = "application not found"


def application_prepare_limit(settings: Settings) -> Limit:
    return Limit(
        "application-prepare",
        settings.rate_limit_application_prepare_per_user,
        settings.rate_limit_application_prepare_window_seconds,
    )


async def _existing_application(session: DbSession, application_id: UUID) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise NotFoundError(_MISSING)
    return application


@router.post(
    "/{application_id}/prepare",
    response_model=PrepareResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request question extraction/classification/grounding",
    dependencies=[Depends(require_owner), Depends(rate_limited(application_prepare_limit))],
)
async def prepare(
    application_id: UUID,
    payload: PrepareRequest,
    session: DbSession,
    owner: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """`resume_ready -> questions_pending`, worked by `workers/application.py`.

    202, not 200: this triggers the worker rather than doing the work
    inline, the same asynchronous shape `POST /jobs/{id}/first-draft` uses.
    """
    result = await request_application_questions(
        session,
        actor=owner,
        application_id=application_id,
        questions=[
            RawQuestion(
                question_text=q.question_text,
                input_type=q.input_type,
                external_field_id=q.external_field_id,
                options=q.options,
                is_required=q.is_required,
                max_length=q.max_length,
            )
            for q in payload.questions
        ],
    )
    await session.commit()
    return PrepareResponse(application_id=result.application_id, event_id=result.event_id)


@router.get(
    "/{application_id}/questions",
    response_model=list[QuestionRead],
    summary="Prepared questions and their current answers",
)
async def list_questions(application_id: UUID, session: DbSession) -> Any:
    """Owner and reviewer alike, matching M2's shared review access - the
    same policy `list_resume_artifacts` already documents for M8."""
    await _existing_application(session, application_id)
    questions = await existing_questions(session, application_id)
    return [QuestionRead.from_model(q) for q in questions]


@router.patch(
    "/{application_id}/answers/{answer_id}",
    response_model=AnswerRead,
    summary="A human sets or overwrites one answer",
)
async def patch_answer(
    application_id: UUID,
    answer_id: UUID,
    payload: EditAnswerRequest,
    session: DbSession,
    actor: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """Always `source = 'human'`, `answered_by = actor.id` - both come from
    the server-side session, never from `payload` (`extra="forbid"` refuses
    an attempt to add either field to the request body)."""
    await _existing_application(session, application_id)
    answer = await edit_answer(
        session,
        application_id=application_id,
        answer_id=answer_id,
        actor=actor,
        value=payload.value,
        value_structured=payload.value_structured,
    )
    await session.commit()
    return AnswerRead.from_model(answer)


@router.post(
    "/{application_id}/package/approve",
    response_model=ApprovalRead,
    summary="Approve the prepared question/answer package",
)
async def approve_package(
    application_id: UUID,
    payload: PackageApproveRequest,
    session: DbSession,
    actor: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """Creates and decides a granted `ApprovalKind.ANSWERS` row in one call -
    see `packages.core.services.applications.approve_answers` for why this
    needs no separate "open" step the way resume review does. Refuses if any
    human-required answer is still unresolved, or if a cited fact is no
    longer verified."""
    await _existing_application(session, application_id)
    approval = await approve_answers(
        session,
        application_id=application_id,
        actor=actor,
        decision_note=payload.decision_note,
    )
    await session.commit()
    return ApprovalRead.from_model(approval)


@router.post(
    "/{application_id}/package/request-changes",
    response_model=ApprovalRead,
    summary="Send the package back with instructions",
)
async def request_package_changes(
    application_id: UUID,
    payload: PackageRequestChangesRequest,
    session: DbSession,
    actor: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """Records the decision only - the application stays `awaiting_approval`
    throughout. Unlike a resume "changes requested", editing an answer
    (`PATCH .../answers/{id}`) needs no pipeline restart."""
    await _existing_application(session, application_id)
    approval = await request_changes_answers(
        session, application_id=application_id, actor=actor, note=payload.note
    )
    await session.commit()
    return ApprovalRead.from_model(approval)
