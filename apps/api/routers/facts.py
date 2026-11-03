"""Profile fact endpoints.

The verification workflow is the whole point of this router. A fact is
created as a draft, a human verifies it, and only then may generated content
cite it. There is no endpoint that creates a verified fact in one step,
because that would make verification a formality rather than a decision.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from apps.api.deps import (
    DbSession,
    authenticated_api,
    fact_suggest_limit,
    rate_limited,
    require_owner,
)
from apps.api.schemas.facts import (
    FactCreate,
    FactEvidenceCreate,
    FactEvidenceRead,
    FactRead,
    FactSuggestRequest,
    FactSummary,
    FactUpdate,
    FactVerifyRequest,
)
from packages.core.models import FactStatus, FactType
from packages.core.repositories import (
    FactEvidenceRepository,
    FactRepository,
    ProfileRepository,
)
from packages.core.services import ProfileService
from packages.shared.errors import ConflictError, ValidationError

router = APIRouter(
    prefix="/profile/facts",
    tags=["profile-facts"],
    dependencies=[Depends(authenticated_api)],
)


async def _profile_id(session: DbSession) -> UUID:
    return (await ProfileRepository(session).get_singleton()).id


@router.get("", response_model=list[FactRead], summary="List facts")
async def list_facts(
    session: DbSession,
    fact_status: Annotated[FactStatus | None, Query(alias="status")] = None,
    fact_type: Annotated[FactType | None, Query()] = None,
    source_table: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    return await FactRepository(session).list_for_profile(
        await _profile_id(session),
        status=fact_status,
        fact_type=fact_type,
        source_table=source_table,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=FactSummary, summary="Fact index summary")
async def fact_summary(session: DbSession) -> Any:
    return await ProfileService(session).fact_summary(await _profile_id(session))


@router.get("/citable", response_model=list[FactRead], summary="Facts a resume may cite")
async def list_citable(session: DbSession) -> Any:
    """Verified, undeleted facts. This is the only set generation may draw on."""
    return await FactRepository(session).list_citable(await _profile_id(session))


@router.post(
    "",
    response_model=FactRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft fact",
    dependencies=[Depends(require_owner)],
)
async def create_fact(payload: FactCreate, session: DbSession) -> Any:
    repo = FactRepository(session)
    fact = await repo.create_draft(
        profile_id=await _profile_id(session),
        fact_type=payload.fact_type,
        text_value=payload.text,
        source_table=payload.source_table,
        source_id=payload.source_id,
        structured=payload.structured,
        confidence=payload.confidence,
    )
    if payload.valid_from or payload.valid_to:
        fact.valid_from = payload.valid_from
        fact.valid_to = payload.valid_to
        await session.flush()
    await session.commit()
    return fact


@router.get("/{fact_id}", response_model=FactRead)
async def get_fact(fact_id: UUID, session: DbSession) -> Any:
    return await FactRepository(session).get_or_404(fact_id)


@router.patch(
    "/{fact_id}",
    response_model=FactRead,
    summary="Edit a draft fact",
    dependencies=[Depends(require_owner)],
)
async def update_fact(fact_id: UUID, payload: FactUpdate, session: DbSession) -> Any:
    repo = FactRepository(session)
    fact = await repo.get_or_404(fact_id)
    if fact.status == FactStatus.VERIFIED:
        # The database refuses this too (trg_profile_facts_protect_verified).
        # Failing here gives a readable message instead of a trigger error.
        raise ConflictError(
            "a verified fact cannot be edited in place; POST /unverify first, "
            "edit, then verify again"
        )
    updated = await repo.update(fact, **payload.model_dump(exclude_unset=True))
    await session.commit()
    return updated


@router.post(
    "/{fact_id}/verify",
    response_model=FactRead,
    summary="Verify a fact (human action)",
    dependencies=[Depends(require_owner)],
)
async def verify_fact(fact_id: UUID, payload: FactVerifyRequest, session: DbSession) -> Any:
    fact = await FactRepository(session).verify(fact_id, verified_by=payload.verified_by)
    await session.commit()
    return fact


@router.post(
    "/{fact_id}/unverify",
    response_model=FactRead,
    summary="Return a fact to draft",
    dependencies=[Depends(require_owner)],
)
async def unverify_fact(fact_id: UUID, session: DbSession) -> Any:
    fact = await FactRepository(session).unverify(fact_id)
    await session.commit()
    return fact


@router.post(
    "/{fact_id}/retire",
    response_model=FactRead,
    summary="Retire a fact that is no longer true",
    dependencies=[Depends(require_owner)],
)
async def retire_fact(fact_id: UUID, session: DbSession) -> Any:
    fact = await FactRepository(session).retire(fact_id)
    await session.commit()
    return fact


@router.delete(
    "/{fact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_fact(fact_id: UUID, session: DbSession) -> Response:
    await FactRepository(session).soft_delete(fact_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/suggest",
    response_model=list[FactRead],
    status_code=status.HTTP_201_CREATED,
    summary="Derive draft facts from an existing profile entry",
    dependencies=[Depends(require_owner), Depends(rate_limited(fact_suggest_limit))],
)
async def suggest_facts(payload: FactSuggestRequest, session: DbSession) -> Any:
    """Deterministic text splitting of what the human already wrote.

    No model is involved and nothing new is asserted: every suggestion is a
    substring of the human's own text, and every one lands as a DRAFT that
    must still be verified before it can be cited.
    """
    if payload.source_table == "profiles":
        raise ValidationError("suggest from a specific profile section, not the profile")
    created = await ProfileService(session).suggest_facts(
        await _profile_id(session), payload.source_table, payload.source_id
    )
    await session.commit()
    return created


# ---------------------------------------------------------------- evidence
@router.get("/{fact_id}/evidence", response_model=list[FactEvidenceRead])
async def list_evidence(fact_id: UUID, session: DbSession) -> Any:
    await FactRepository(session).get_or_404(fact_id)
    return await FactEvidenceRepository(session).list_for_fact(fact_id)


@router.post(
    "/{fact_id}/evidence",
    response_model=FactEvidenceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def add_evidence(fact_id: UUID, payload: FactEvidenceCreate, session: DbSession) -> Any:
    await FactRepository(session).get_or_404(fact_id)
    if not any((payload.url, payload.note, payload.artifact_id)):
        raise ValidationError("evidence needs at least a url, a note, or an artifact")
    evidence = await FactEvidenceRepository(session).create(fact_id=fact_id, **payload.model_dump())
    await session.commit()
    return evidence
