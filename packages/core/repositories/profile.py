"""Profile and profile sub-entity repositories."""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from packages.core.db.base import Base
from packages.core.models import (
    Certification,
    Education,
    Experience,
    Profile,
    Project,
    Skill,
)
from packages.core.repositories.base import BaseRepository
from packages.shared.clock import utc_now
from packages.shared.errors import NotFoundError

ChildT = TypeVar("ChildT", bound=Base)


class ProfileRepository(BaseRepository[Profile]):
    model = Profile

    async def get_by_user(self, user_id: UUID) -> Profile | None:
        stmt = select(Profile).where(Profile.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_children(self, profile_id: UUID) -> Profile | None:
        stmt = (
            select(Profile)
            .where(Profile.id == profile_id)
            .options(
                selectinload(Profile.experiences),
                selectinload(Profile.educations),
                selectinload(Profile.certifications),
                selectinload(Profile.skills),
                selectinload(Profile.projects),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_singleton(self) -> Profile:
        """This is a single-candidate system: there is one profile."""
        # Ordered so the answer is stable: an UPDATE moves a row in the heap.
        stmt = select(Profile).order_by(Profile.created_at, Profile.id).limit(1)
        profile = (await self.session.execute(stmt)).scalar_one_or_none()
        if profile is None:
            raise NotFoundError("no profile exists yet")
        return profile

    async def bump_version(self, profile: Profile) -> Profile:
        """Increment the profile version.

        Job evaluations record which profile version they were computed
        against, so this must move whenever the underlying facts change.
        """
        profile.version += 1
        await self.session.flush()
        return profile


class _ProfileChildRepository(BaseRepository[ChildT]):
    """Shared behaviour for the five profile sub-entity tables."""

    async def list_for_profile(
        self, profile_id: UUID, include_deleted: bool = False
    ) -> list[ChildT]:
        stmt = (
            self._base_select(include_deleted)
            .where(self.model.profile_id == profile_id)  # type: ignore[attr-defined]
            .order_by(
                self.model.display_order,  # type: ignore[attr-defined]
                self.model.created_at.desc(),  # type: ignore[attr-defined]
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def soft_delete(self, entity: ChildT) -> ChildT:
        """Profile history is soft-deleted, never hard-deleted.

        A fact may cite this row, and losing the provenance of a claim that
        already went out on a resume would break the audit trail.
        """
        entity.deleted_at = utc_now()  # type: ignore[attr-defined]
        await self.session.flush()
        return entity

    async def create_for_profile(self, profile_id: UUID, **values: Any) -> ChildT:
        return await self.create(profile_id=profile_id, **values)


class ExperienceRepository(_ProfileChildRepository[Experience]):
    model = Experience


class EducationRepository(_ProfileChildRepository[Education]):
    model = Education


class CertificationRepository(_ProfileChildRepository[Certification]):
    model = Certification


class SkillRepository(_ProfileChildRepository[Skill]):
    model = Skill


class ProjectRepository(_ProfileChildRepository[Project]):
    model = Project


#: Maps the API path segment to its repository and model, so the routers stay
#: thin instead of repeating five near-identical CRUD blocks.
CHILD_REPOSITORIES: dict[str, type[_ProfileChildRepository[Any]]] = {
    "experiences": ExperienceRepository,
    "educations": EducationRepository,
    "certifications": CertificationRepository,
    "skills": SkillRepository,
    "projects": ProjectRepository,
}
