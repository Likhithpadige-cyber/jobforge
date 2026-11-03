"""Private, human-authored master-resume boundary; importing never verifies facts."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.llm.runtime import canonical
from packages.core.events import emit
from packages.core.models import Profile, ProfileFact, Resume, User
from packages.shared.errors import ForbiddenError, NotFoundError, ValidationError


class MasterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    fact_ids: list[UUID] = Field(default_factory=list, max_length=20)


class MasterSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["experience", "skills", "projects", "education", "certifications", "summary"]
    entries: list[MasterEntry] = Field(min_length=1, max_length=100)


class MasterResumeData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    source_label: str = Field(min_length=1, max_length=120)
    sections: list[MasterSection] = Field(min_length=1, max_length=12)


async def check_master(
    session: AsyncSession,
    profile_id: UUID,
    data: MasterResumeData,
) -> set[UUID]:
    if await session.get(Profile, profile_id) is None:
        raise NotFoundError("candidate or evidence not found")
    ids = {fid for section in data.sections for entry in section.entries for fid in entry.fact_ids}
    if ids:
        facts = list(
            (
                await session.scalars(
                    select(ProfileFact).where(
                        ProfileFact.id.in_(ids),
                    )
                )
            ).all()
        )
        if len(facts) != len(ids) or any(
            f.profile_id != profile_id or f.deleted_at is not None for f in facts
        ):
            raise NotFoundError("candidate or evidence not found")
    if len(canonical(data.model_dump(mode="json")).encode()) > 200_000:
        raise ValidationError("master resume exceeds the structured import size limit")
    return ids


async def import_master(
    session: AsyncSession, *, actor: User, profile_id: UUID, data: MasterResumeData
) -> Resume:
    if actor.role != "owner":
        raise ForbiddenError("owner role required")
    # Serializes append-only version allocation without introducing a schema.
    profile = await session.scalar(
        select(Profile)
        .where(
            Profile.id == profile_id,
        )
        .with_for_update()
    )
    if profile is None:
        raise NotFoundError("candidate or evidence not found")
    await check_master(session, profile_id, data)
    content = data.model_dump(mode="json")
    latest = await session.scalar(
        select(Resume)
        .where(
            Resume.profile_id == profile_id,
            Resume.kind == "base",
        )
        .order_by(Resume.version.desc(), Resume.id.desc())
        .limit(1)
    )
    if latest is not None and latest.content == content:
        return latest
    version = await session.scalar(
        select(func.max(Resume.version)).where(
            Resume.profile_id == profile_id,
            Resume.kind == "base",
        )
    )
    resume = Resume(
        profile_id=profile_id,
        kind="base",
        version=(version or 0) + 1,
        content=content,
        verification_status="pending",
    )
    session.add(resume)
    await session.flush()
    await emit(
        session,
        type="resume.master.imported",
        aggregate_type="resume",
        aggregate_id=resume.id,
        correlation_id=profile_id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "profile_id": str(profile_id),
            "resume_id": str(resume.id),
            "version": resume.version,
            "content_digest": hashlib.sha256(canonical(content).encode()).hexdigest(),
        },
    )
    return resume
