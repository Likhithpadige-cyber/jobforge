"""Read access to base resume structures."""
from uuid import UUID

from sqlalchemy import select

from packages.core.models import Resume
from packages.core.repositories.base import BaseRepository


class ResumeRepository(BaseRepository[Resume]):
    model = Resume

    async def latest_base_for_profile(self, profile_id: UUID) -> Resume | None:
        stmt = (
            select(Resume)
            .where(Resume.profile_id == profile_id, Resume.kind == "base")
            .order_by(Resume.version.desc(), Resume.created_at.desc(), Resume.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
