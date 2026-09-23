"""M9 - application preparation: extraction, review and the answers approval.
M10 Phase 2 adds the handoff-packet download at the bottom of this file.
M11 Phase 2 adds the owner-only human submission endpoint.

    POST  /api/v1/applications/{id}/prepare               owner only
    GET   /api/v1/applications/{id}/questions              owner + reviewer
    PATCH /api/v1/applications/{id}/answers/{answer_id}    owner + reviewer
    POST  /api/v1/applications/{id}/package/approve        owner + reviewer
    POST  /api/v1/applications/{id}/package/request-changes owner + reviewer
    GET   /api/v1/applications/{id}/packet                 owner + reviewer
    POST  /api/v1/applications/{id}/submit                 owner only

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

from fastapi import APIRouter, Depends, Response, status

from apps.api.deps import (
    AppSettings,
    CsrfProtected,
    CurrentHumanActor,
    DbSession,
    RequireOwner,
    artifact_download_limit,
    artifact_store,
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
    SubmitRequest,
    SubmitResponse,
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
from packages.core.services.handoff_packet import PACKET_CONTENT_TYPE, current_packet
from packages.core.services.submission_execution import request_submission
from packages.services.rendering.keys import sanitize_filename
from packages.services.rendering.store import ArtifactIntegrityError
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


@router.get(
    "/{application_id}/packet",
    summary="Download the handoff packet",
    response_class=Response,
    dependencies=[Depends(rate_limited(artifact_download_limit))],
)
async def download_packet(
    application_id: UUID, session: DbSession, settings: AppSettings
) -> Response:
    """The bytes, with the checksum re-checked on the way out - the same
    three-step discipline `download_artifact` uses in `artifacts.py`: read
    the row, validate the content type against a fixed value rather than
    echoing it, re-hash before returning. Serves the packet M10 Phase 2's
    readiness step already generated; does not generate one on the fly.

    Owner and reviewer alike, matching every other application-review route
    in this file - this system has no per-resource ACL beyond that role
    check (SECURITY.md's own threat model excludes multi-tenant isolation).
    """
    await _existing_application(session, application_id)
    artifact = await current_packet(session, application_id)
    if artifact is None:
        raise NotFoundError(
            "no handoff packet exists yet for this application"
        )
    if artifact.content_type != PACKET_CONTENT_TYPE:
        raise ArtifactIntegrityError("packet artifact has an unsupported content type")

    store = artifact_store(settings)
    data = await store.get(artifact.storage_key)
    if artifact.sha256:
        await store.verify(artifact.storage_key, sha256=artifact.sha256)

    filename = sanitize_filename(f"jobforge-packet-{application_id}.zip")
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/{application_id}/submit",
    response_model=SubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request human-authorized email submission",
    dependencies=[Depends(require_owner)],
)
async def submit_application(
    application_id: UUID,
    session: DbSession,
    owner: RequireOwner,
    settings: AppSettings,
    _csrf: CsrfProtected,
    _payload: SubmitRequest | None = None,
) -> SubmitResponse:
    """Human-authorized email submission (M11 Phase 2).

    Consequential external action: OWNER only, CSRF protected.
    Authenticates before resource lookup, answers missing applications with 404
    (preserving non-enumeration). Preconditions verified atomically.
    Destination email is derived solely from authoritative persisted job data (job.url mailto:).
    Transitions to SUBMITTING and emits submission.requested; performs zero
    network/SMTP I/O in the request path.
    """
    submission = await request_submission(
        session,
        application_id,
        settings,
        actor=owner,
    )
    await session.commit()
    return SubmitResponse(
        application_id=application_id,
        submission_id=submission.id,
        status="submitting",
        attempt=submission.attempt,
    )
