"""Profile service: business rules that span more than one repository."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

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
)
from packages.core.repositories import (
    CHILD_REPOSITORIES,
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
