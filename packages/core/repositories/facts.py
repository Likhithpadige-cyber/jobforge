"""Profile fact repository.

Everything here exists to keep one promise: a generated resume can only say
things the human has verified. The database enforces it (grants + triggers);
this layer makes the enforced behaviour easy to use correctly.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import FactEvidence, FactStatus, FactType, ProfileFact
from packages.core.repositories.base import BaseRepository
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.clock import utc_now


class FactRepository(BaseRepository[ProfileFact]):
    model = ProfileFact

    async def list_for_profile(
        self,
        profile_id: UUID,
        status: FactStatus | None = None,
        fact_type: FactType | None = None,
        source_table: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ProfileFact]:
        stmt = self._base_select().where(ProfileFact.profile_id == profile_id)
        if status is not None:
            stmt = stmt.where(ProfileFact.status == status)
        if fact_type is not None:
            stmt = stmt.where(ProfileFact.fact_type == fact_type)
        if source_table is not None:
            stmt = stmt.where(ProfileFact.source_table == source_table)
        stmt = stmt.order_by(ProfileFact.created_at.desc()).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_citable(self, profile_id: UUID) -> list[ProfileFact]:
        """The only facts a resume may draw on."""
        stmt = (
            self._base_select()
            .where(ProfileFact.profile_id == profile_id)
            .where(ProfileFact.status == FactStatus.VERIFIED)
            .order_by(ProfileFact.fact_type, ProfileFact.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self, profile_id: UUID) -> dict[str, int]:
        stmt = (
            select(ProfileFact.status, func.count())
            .where(ProfileFact.profile_id == profile_id)
            .where(ProfileFact.deleted_at.is_(None))
            .group_by(ProfileFact.status)
        )
        rows = (await self.session.execute(stmt)).all()
        counts = {status.value: 0 for status in FactStatus}
        for status, count in rows:
            counts[str(status)] = count
        return counts

    async def create_draft(
        self,
        profile_id: UUID,
        fact_type: FactType,
        text_value: str,
        source_table: str,
        source_id: UUID,
        structured: dict | None = None,
        confidence: str | None = None,
    ) -> ProfileFact:
        """Create a fact in DRAFT.

        Facts are never born verified. Verification is a separate, deliberate
        human action recorded with a user id and a timestamp, so that
        "verified" always names a person and a moment.
        """
        if not text_value.strip():
            raise ValidationError("fact text cannot be blank")
        return await self.create(
            profile_id=profile_id,
            fact_type=fact_type,
            text=text_value.strip(),
            structured=structured or {},
            source_table=source_table,
            source_id=source_id,
            status=FactStatus.DRAFT,
            confidence=confidence,
        )

    async def verify(self, fact_id: UUID, verified_by: UUID) -> ProfileFact:
        """Promote a draft to verified. Human action only."""
        fact = await self.get(fact_id)
        if fact is None:
            raise NotFoundError(f"fact {fact_id} not found")
        if fact.status == FactStatus.VERIFIED:
            raise ConflictError(f"fact {fact_id} is already verified")
        if fact.status == FactStatus.RETIRED:
            raise ConflictError(f"fact {fact_id} is retired; un-retire it first")

        fact.status = FactStatus.VERIFIED
        fact.verified_by = verified_by
        fact.verified_at = utc_now()
        await self.session.flush()
        return fact

    async def unverify(self, fact_id: UUID) -> ProfileFact:
        """Return a fact to draft so its text can be corrected.

        The database blocks editing the substance of a verified fact in place
        (trg_profile_facts_protect_verified), so this is the only route to
        changing what a verified fact says - and it means anything that cited
        the old wording is now citing a fact that is no longer citable.
        """
        fact = await self.get(fact_id)
        if fact is None:
            raise NotFoundError(f"fact {fact_id} not found")
        fact.status = FactStatus.DRAFT
        fact.verified_by = None
        fact.verified_at = None
        await self.session.flush()
        return fact

    async def retire(self, fact_id: UUID) -> ProfileFact:
        """Retire a fact that is no longer true. It stops being citable."""
        fact = await self.get(fact_id)
        if fact is None:
            raise NotFoundError(f"fact {fact_id} not found")
        fact.status = FactStatus.RETIRED
        await self.session.flush()
        return fact

    async def soft_delete(self, fact_id: UUID) -> ProfileFact:
        fact = await self.get(fact_id)
        if fact is None:
            raise NotFoundError(f"fact {fact_id} not found")
        fact.deleted_at = utc_now()
        await self.session.flush()
        return fact

    async def increment_usage(self, fact_id: UUID) -> int:
        """Bump usage_count via the SECURITY DEFINER function.

        Agent roles hold no UPDATE on profile_facts, so they cannot write this
        column directly. The function is the single narrow exception.
        """
        stmt = select(
            text("jobforge_increment_fact_usage(:fact_id)").bindparams(fact_id=fact_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def verify_all_citable(self, fact_ids: list[UUID]) -> list[UUID]:
        """Return the subset of ``fact_ids`` that are NOT citable.

        Used before building a claim so the caller gets a clear error rather
        than a trigger exception. The trigger remains the real enforcement.
        """
        if not fact_ids:
            return []
        stmt = (
            select(ProfileFact.id)
            .where(ProfileFact.id.in_(fact_ids))
            .where(ProfileFact.status == FactStatus.VERIFIED)
            .where(ProfileFact.deleted_at.is_(None))
        )
        found = {row[0] for row in (await self.session.execute(stmt)).all()}
        return [fact_id for fact_id in fact_ids if fact_id not in found]


class FactEvidenceRepository(BaseRepository[FactEvidence]):
    model = FactEvidence

    async def list_for_fact(self, fact_id: UUID) -> list[FactEvidence]:
        stmt = (
            select(FactEvidence)
            .where(FactEvidence.fact_id == fact_id)
            .order_by(FactEvidence.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())


def build_fact_repository(session: AsyncSession) -> FactRepository:
    return FactRepository(session)
