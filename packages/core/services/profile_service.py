"""Profile service: business rules that span more than one repository."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    Certification,
    Education,
    Experience,
    FactStatus,
    FactType,
    Profile,
    ProfileFact,
    Project,
    Skill,
    User,
)
from packages.core.repositories import (
    CHILD_REPOSITORIES,
    FactEvidenceRepository,
    FactRepository,
    ProfileRepository,
)
from packages.shared.errors import ConflictError, NotFoundError, ValidationError

#: Which fact_type a suggestion from each source table gets.
SOURCE_FACT_TYPE: dict[str, FactType] = {
    "experiences": FactType.EXPERIENCE,
    "educations": FactType.EDUCATION,
    "certifications": FactType.CERTIFICATION,
    "skills": FactType.SKILL,
    "projects": FactType.PROJECT,
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMERAL = re.compile(r"\d[\d,.]*%?")


class ProfileService:
    """Coordinates the profile, its sub-entities, and the fact index."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.profiles = ProfileRepository(session)
        self.facts = FactRepository(session)

    # ------------------------------------------------------------ profile
    async def get_profile(self, profile_id: UUID) -> Profile:
        profile = await self.profiles.get_with_children(profile_id)
        if profile is None:
            raise NotFoundError(f"profile {profile_id} not found")
        return profile

    async def update_profile(self, profile_id: UUID, **values: Any) -> Profile:
        profile = await self.profiles.get_or_404(profile_id)
        expected_version = values.pop("version", None)
        if expected_version is not None and expected_version != profile.version:
            # A stale copy is a conflict (409), not malformed input: the UI
            # tells the person to reload rather than to fix a field.
            raise ConflictError(
                f"profile has moved on (expected version {expected_version}, "
                f"current {profile.version}); reload before saving"
            )
        for key, value in values.items():
            if value is not None:
                setattr(profile, key, value)
        await self.profiles.bump_version(profile)
        return profile

    async def _valid_fact_source(self, profile_id: UUID, kind: str, source_id: UUID) -> bool:
        if kind == "profiles":
            if source_id != profile_id:
                return False
            record = await self.session.get(Profile, source_id, populate_existing=True)
            return record is not None and record.id == profile_id
        if kind not in CHILD_REPOSITORIES:
            return False
        model = CHILD_REPOSITORIES[kind].model
        child = await self.session.get(model, source_id, populate_existing=True)
        return bool(
            child is not None and child.profile_id == profile_id and child.deleted_at is None
        )

    async def validate_fact_source(
        self, profile_id: UUID, kind: str, source_id: UUID, *, actor: User | None = None
    ) -> None:
        # Serialize with supported profile writes via the parent lock. Child
        # CRUD takes its row before bumping the profile, so do not invert that
        # order by taking a child write lock while holding the profile.
        profile = await self.session.get(Profile, profile_id, with_for_update=True)
        if actor is not None and (
            profile is None or actor.role != "owner" or profile.user_id != actor.id
        ):
            raise NotFoundError("fact source not found")
        if not await self._valid_fact_source(profile_id, kind, source_id):
            raise ValidationError(
                "Fact source must be an existing, active record on this profile; "
                "create or select the correct profile entry first"
            )

    async def repair_fact_source(
        self,
        *,
        profile_id: UUID,
        fact_id: UUID,
        source_id: UUID,
        expected_updated_at: datetime,
        note: str,
        actor: User,
    ) -> ProfileFact:
        """Owner explicitly replaces dangling evidence; original content and link survive."""
        profile = await self.session.get(Profile, profile_id, with_for_update=True)
        if profile is None or actor.role != "owner" or profile.user_id != actor.id:
            raise NotFoundError("fact source repair not found")
        original = await self.session.scalar(
            select(ProfileFact)
            .where(
                ProfileFact.id == fact_id,
                ProfileFact.profile_id == profile_id,
                ProfileFact.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if original is None:
            raise NotFoundError("fact source repair not found")
        if not note.strip():
            raise ValidationError("Source repair requires an owner explanation")
        if original.updated_at != expected_updated_at or original.status == FactStatus.RETIRED:
            raise ConflictError("Fact changed or was already retired; reload before source repair")
        if await self._valid_fact_source(profile_id, original.source_table, original.source_id):
            raise ConflictError(
                "The source is still valid; source repair is only for dangling references"
            )
        await self.validate_fact_source(profile_id, original.source_table, source_id)
        duplicate = await self.session.scalar(
            select(ProfileFact.id).where(
                ProfileFact.profile_id == profile_id,
                ProfileFact.source_table == original.source_table,
                ProfileFact.source_id == source_id,
                ProfileFact.text == original.text,
                ProfileFact.deleted_at.is_(None),
            )
        )
        if duplicate is not None:
            raise ConflictError("Replacement evidence already exists; review that fact instead")
        replacement = await self.facts.create_draft(
            profile_id=profile_id,
            fact_type=original.fact_type,
            text_value=original.text,
            source_table=original.source_table,
            source_id=source_id,
            structured=original.structured,
            confidence=original.confidence,
        )
        replacement.valid_from, replacement.valid_to = original.valid_from, original.valid_to
        # Never update the approved fact's text, source or ID, or relink resume claims.
        original.status = FactStatus.RETIRED
        history = (
            f"Owner-authorized source repair: original {original.id} "
            f"({original.source_table}/{original.source_id}); replacement {replacement.id} "
            f"({replacement.source_table}/{replacement.source_id}). Reason: {note.strip()}"
        )
        evidence = FactEvidenceRepository(self.session)
        for fact in (original, replacement):
            await evidence.create(
                fact_id=fact.id, kind="attestation", note=history, added_by=actor.id
            )
        await self.session.flush()
        return replacement

    # ------------------------------------------------- fact suggestions
    def _suggest_texts(self, entity: Any, kind: str) -> list[tuple[str, dict[str, Any]]]:
        """Split a human-authored description into candidate fact sentences.

        This is deterministic text splitting, not generation. Nothing new is
        asserted: every suggestion is a substring of what the human wrote, and
        every one lands as a DRAFT that a human must verify before it can be
        cited. No model is involved at any point.
        """
        suggestions: list[tuple[str, dict[str, Any]]] = []

        if kind == "experiences" and isinstance(entity, Experience):
            header = f"{entity.title} at {entity.company_name}"
            period = {
                "start_date": entity.start_date.isoformat(),
                "end_date": entity.end_date.isoformat() if entity.end_date else None,
            }
            suggestions.append((header, period))
            for sentence in _SENTENCE_SPLIT.split(entity.description or ""):
                cleaned = sentence.strip(" -•\t")
                if len(cleaned) > 10:
                    suggestions.append((cleaned, {"numerals": _NUMERAL.findall(cleaned), **period}))
        elif kind == "educations" and isinstance(entity, Education):
            parts = [p for p in (entity.degree, entity.field_of_study) if p]
            label = " in ".join(parts) if parts else "Studied"
            suggestions.append(
                (
                    f"{label} at {entity.institution}",
                    {"end_date": entity.end_date.isoformat() if entity.end_date else None},
                )
            )
        elif kind == "certifications" and isinstance(entity, Certification):
            # An in-progress certification is never suggested as one held.
            if entity.status == "active":
                suggestions.append(
                    (
                        f"Holds {entity.name}"
                        + (f" from {entity.issuer}" if entity.issuer else ""),
                        {
                            "issued_date": entity.issued_date.isoformat()
                            if entity.issued_date
                            else None
                        },
                    )
                )
        elif kind == "skills" and isinstance(entity, Skill):
            level = f" at a {entity.proficiency} level" if entity.proficiency else ""
            suggestions.append((f"Uses {entity.name}{level}", {}))
        elif kind == "projects" and isinstance(entity, Project):
            suggestions.append((f"Built {entity.name}", {"tech_stack": list(entity.tech_stack)}))
            if entity.summary:
                suggestions.append(
                    (entity.summary.strip(), {"numerals": _NUMERAL.findall(entity.summary)})
                )
        return suggestions

    async def suggest_facts(
        self, profile_id: UUID, kind: str, entity_id: UUID
    ) -> list[ProfileFact]:
        """Create DRAFT facts from an existing profile entity.

        Returns the created drafts. They are not citable until a human calls
        verify() on each one.
        """
        if kind not in CHILD_REPOSITORIES:
            raise ValidationError(f"unknown profile section '{kind}'")

        repo = CHILD_REPOSITORIES[kind](self.session)
        entity = await repo.get(entity_id)
        if entity is None or entity.profile_id != profile_id:
            raise NotFoundError(f"{kind} {entity_id} not found on this profile")

        existing = {
            fact.text for fact in await self.facts.list_for_profile(profile_id, source_table=kind)
        }

        created: list[ProfileFact] = []
        for text_value, structured in self._suggest_texts(entity, kind):
            if text_value in existing:
                continue
            created.append(
                await self.facts.create_draft(
                    profile_id=profile_id,
                    fact_type=SOURCE_FACT_TYPE[kind],
                    text_value=text_value,
                    source_table=kind,
                    source_id=entity_id,
                    structured=structured,
                )
            )
        return created

    # ------------------------------------------------------------ summary
    async def fact_summary(self, profile_id: UUID) -> dict[str, Any]:
        counts = await self.facts.counts_by_status(profile_id)
        citable = await self.facts.list_citable(profile_id)
        by_type: dict[str, int] = {}
        never_used = 0
        for fact in citable:
            by_type[str(fact.fact_type)] = by_type.get(str(fact.fact_type), 0) + 1
            if fact.usage_count == 0:
                never_used += 1
        return {
            "total": sum(counts.values()),
            "by_status": counts,
            "citable": len(citable),
            "citable_by_type": by_type,
            "never_used": never_used,
            "draft_awaiting_verification": counts.get(FactStatus.DRAFT.value, 0),
        }
