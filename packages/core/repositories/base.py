"""Repository base.

Repositories own all query construction. API routes call repositories; they
never build a query themselves (ARCHITECTURE.md §12 keeps the domain layer
free of framework concerns).
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.db.base import Base
from packages.shared.errors import NotFoundError

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """CRUD shared by every repository.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded by default on
    models that have the column, so a caller cannot resurrect deleted profile
    data by forgetting a filter.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @property
    def _soft_deletes(self) -> bool:
        return hasattr(self.model, "deleted_at")

    def _base_select(self, include_deleted: bool = False) -> Select[tuple[ModelT]]:
        stmt = select(self.model)
        if self._soft_deletes and not include_deleted:
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    async def get(self, entity_id: UUID, include_deleted: bool = False) -> ModelT | None:
        stmt = self._base_select(include_deleted).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_404(self, entity_id: UUID) -> ModelT:
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFoundError(f"{self.model.__tablename__} {entity_id} not found")
        return entity

    async def list(self, limit: int = 100, offset: int = 0) -> list[ModelT]:
        stmt = self._base_select().limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        if self._soft_deletes:
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one()

    async def create(self, **values: Any) -> ModelT:
        entity = self.model(**values)
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def update(self, entity: ModelT, **values: Any) -> ModelT:
        for key, value in values.items():
            if value is not None or key in getattr(entity, "__nullable_fields__", ()):
                setattr(entity, key, value)
        await self.session.flush()
        return entity
