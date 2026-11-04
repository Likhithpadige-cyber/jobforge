"""Profile endpoints (ARCHITECTURE.md §5.4).

Routes stay thin: they validate, delegate to a repository or service, and
shape the response. No query construction lives here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from apps.api.deps import CurrentUser, DbSession, authenticated_api, require_owner
from apps.api.schemas.profile import (
    CertificationCreate,
    CertificationRead,
    CertificationUpdate,
    EducationCreate,
    EducationRead,
    EducationUpdate,
    ExperienceCreate,
    ExperienceRead,
    ExperienceUpdate,
    ProfileDetail,
    ProfileRead,
    ProfileUpdate,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    SkillCreate,
    SkillRead,
    SkillUpdate,
)
from packages.core.repositories import (
    CertificationRepository,
    EducationRepository,
    ExperienceRepository,
    ProfileRepository,
    ProjectRepository,
    SkillRepository,
)
from packages.core.services import ProfileService
from packages.shared.errors import NotFoundError

router = APIRouter(prefix="/profile", tags=["profile"], dependencies=[Depends(authenticated_api)])


#: Profile fields only the owner may read.
OWNER_ONLY_PROFILE_FIELDS = ("email", "phone", "work_authorization", "min_salary", "max_salary")


@router.get("", response_model=ProfileDetail, summary="Get the profile")
async def get_profile(session: DbSession, user: CurrentUser) -> Any:
    repo = ProfileRepository(session)
    profile = await repo.get_singleton()
    detailed = await repo.get_with_children(profile.id)
    if detailed is None:  # pragma: no cover - the row was just read
        raise NotFoundError("profile not found")
    view = ProfileDetail.model_validate(detailed)
    if user.role == "owner":
        return view
    # Reviewers check materials against facts; they never need the
    # candidate's contact details, work authorization or salary range.
    return view.model_copy(update=dict.fromkeys(OWNER_ONLY_PROFILE_FIELDS))


@router.put(
    "",
    response_model=ProfileRead,
    summary="Update the profile",
    dependencies=[Depends(require_owner)],
)
async def update_profile(payload: ProfileUpdate, session: DbSession) -> Any:
    service = ProfileService(session)
    profile = await service.profiles.get_singleton()
    updated = await service.update_profile(profile.id, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


# ---------------------------------------------------------------------------
# The five sub-entity collections share one shape. Each is written out rather
# than generated, because explicit routes give correct OpenAPI schemas - and
# the frontend types are generated from that schema.
# ---------------------------------------------------------------------------


async def _saved(session: DbSession, entity: Any) -> Any:
    """Commit, then reload server-generated columns (e.g. `updated_at`).

    `updated_at` is set by the database on UPDATE, so after a flush the ORM
    attribute is expired; reading it while serialising would need IO outside
    the async context. Refreshing here keeps the response complete.
    """
    await session.commit()
    await session.refresh(entity)
    return entity


async def _profile_id(session: DbSession) -> UUID:
    return (await ProfileRepository(session).get_singleton()).id


# ------------------------------------------------------------- experiences
@router.get("/experiences", response_model=list[ExperienceRead])
async def list_experiences(session: DbSession) -> Any:
    return await ExperienceRepository(session).list_for_profile(await _profile_id(session))


@router.post(
    "/experiences",
    response_model=ExperienceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_experience(payload: ExperienceCreate, session: DbSession) -> Any:
    entity = await ExperienceRepository(session).create_for_profile(
        await _profile_id(session), **payload.model_dump()
    )
    return await _saved(session, entity)


@router.patch(
    "/experiences/{entity_id}",
    response_model=ExperienceRead,
    dependencies=[Depends(require_owner)],
)
async def update_experience(entity_id: UUID, payload: ExperienceUpdate, session: DbSession) -> Any:
    repo = ExperienceRepository(session)
    entity = await repo.get_or_404(entity_id)
    updated = await repo.update(entity, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


@router.delete(
    "/experiences/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_experience(entity_id: UUID, session: DbSession) -> Response:
    repo = ExperienceRepository(session)
    await repo.soft_delete(await repo.get_or_404(entity_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -------------------------------------------------------------- educations
@router.get("/educations", response_model=list[EducationRead])
async def list_educations(session: DbSession) -> Any:
    return await EducationRepository(session).list_for_profile(await _profile_id(session))


@router.post(
    "/educations",
    response_model=EducationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_education(payload: EducationCreate, session: DbSession) -> Any:
    entity = await EducationRepository(session).create_for_profile(
        await _profile_id(session), **payload.model_dump()
    )
    return await _saved(session, entity)


@router.patch(
    "/educations/{entity_id}",
    response_model=EducationRead,
    dependencies=[Depends(require_owner)],
)
async def update_education(entity_id: UUID, payload: EducationUpdate, session: DbSession) -> Any:
    repo = EducationRepository(session)
    entity = await repo.get_or_404(entity_id)
    updated = await repo.update(entity, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


@router.delete(
    "/educations/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_education(entity_id: UUID, session: DbSession) -> Response:
    repo = EducationRepository(session)
    await repo.soft_delete(await repo.get_or_404(entity_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------- certifications
@router.get("/certifications", response_model=list[CertificationRead])
async def list_certifications(session: DbSession) -> Any:
    return await CertificationRepository(session).list_for_profile(await _profile_id(session))


@router.post(
    "/certifications",
    response_model=CertificationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_certification(payload: CertificationCreate, session: DbSession) -> Any:
    entity = await CertificationRepository(session).create_for_profile(
        await _profile_id(session), **payload.model_dump()
    )
    return await _saved(session, entity)


@router.patch(
    "/certifications/{entity_id}",
    response_model=CertificationRead,
    dependencies=[Depends(require_owner)],
)
async def update_certification(
    entity_id: UUID, payload: CertificationUpdate, session: DbSession
) -> Any:
    repo = CertificationRepository(session)
    entity = await repo.get_or_404(entity_id)
    updated = await repo.update(entity, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


@router.delete(
    "/certifications/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_certification(entity_id: UUID, session: DbSession) -> Response:
    repo = CertificationRepository(session)
    await repo.soft_delete(await repo.get_or_404(entity_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ skills
@router.get("/skills", response_model=list[SkillRead])
async def list_skills(session: DbSession) -> Any:
    return await SkillRepository(session).list_for_profile(await _profile_id(session))


@router.post(
    "/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_skill(payload: SkillCreate, session: DbSession) -> Any:
    entity = await SkillRepository(session).create_for_profile(
        await _profile_id(session), **payload.model_dump()
    )
    return await _saved(session, entity)


@router.patch(
    "/skills/{entity_id}",
    response_model=SkillRead,
    dependencies=[Depends(require_owner)],
)
async def update_skill(entity_id: UUID, payload: SkillUpdate, session: DbSession) -> Any:
    repo = SkillRepository(session)
    entity = await repo.get_or_404(entity_id)
    updated = await repo.update(entity, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


@router.delete(
    "/skills/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_skill(entity_id: UUID, session: DbSession) -> Response:
    repo = SkillRepository(session)
    await repo.soft_delete(await repo.get_or_404(entity_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- projects
@router.get("/projects", response_model=list[ProjectRead])
async def list_projects(session: DbSession) -> Any:
    return await ProjectRepository(session).list_for_profile(await _profile_id(session))


@router.post(
    "/projects",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_project(payload: ProjectCreate, session: DbSession) -> Any:
    entity = await ProjectRepository(session).create_for_profile(
        await _profile_id(session), **payload.model_dump()
    )
    return await _saved(session, entity)


@router.patch(
    "/projects/{entity_id}",
    response_model=ProjectRead,
    dependencies=[Depends(require_owner)],
)
async def update_project(entity_id: UUID, payload: ProjectUpdate, session: DbSession) -> Any:
    repo = ProjectRepository(session)
    entity = await repo.get_or_404(entity_id)
    updated = await repo.update(entity, **payload.model_dump(exclude_unset=True))
    return await _saved(session, updated)


@router.delete(
    "/projects/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_project(entity_id: UUID, session: DbSession) -> Response:
    repo = ProjectRepository(session)
    await repo.soft_delete(await repo.get_or_404(entity_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
