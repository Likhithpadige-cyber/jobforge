#!/usr/bin/env python3
"""Seed demo development data.

Everything here is fictional. No real person, employer, credential or contact
detail appears in this file, and none ever should: this script is committed,
and the real profile belongs only in your local database (SECURITY.md §6).

    make seed

Idempotent: re-running does not duplicate rows.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from sqlalchemy import text

from packages.core.cache import check_redis, close_redis, init_redis
from packages.core.db import (
    check_database,
    dispose_engine,
    get_migration_revision,
    init_engine,
    session_scope,
)
from packages.core.models import FactType
from packages.core.repositories import (
    CertificationRepository,
    EducationRepository,
    ExperienceRepository,
    FactRepository,
    ProjectRepository,
    SkillRepository,
)
from packages.shared.config import get_settings
from packages.shared.ids import uuid7

DEMO_EMAIL = "demo.candidate@example.com"


async def seed() -> int:
    async with session_scope() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": DEMO_EMAIL}
            )
        ).scalar_one_or_none()
        if existing is not None:
            print("demo data already present; nothing to do")
            return 0

        user_id, profile_id = uuid7(), uuid7()
        await session.execute(
            text("INSERT INTO users (id, email, role) VALUES (:id, :email, 'owner')"),
            {"id": user_id, "email": DEMO_EMAIL},
        )
        await session.execute(
            text(
                "INSERT INTO profiles (id, user_id, full_name, headline, "
                "location_city, location_state, location_country, "
                "willing_to_relocate, target_roles, links) "
                "VALUES (:id, :user_id, 'Demo Candidate', "
                "'Demo profile for local development', 'Example City', 'EX', 'US', "
                "false, ARRAY['Cloud Engineer','DevOps Engineer'], '{}'::jsonb)"
            ),
            {"id": profile_id, "user_id": user_id},
        )
        await session.flush()

        experience = await ExperienceRepository(session).create_for_profile(
            profile_id,
            company_name="Example Cloud Corp",
            title="Demo Cloud Engineer",
            employment_type="full_time",
            location="Example City, EX",
            start_date=date(2024, 3, 1),
            description=(
                "Maintained demo Linux servers. " "Monitored demo workloads with a demo dashboard."
            ),
        )
        await EducationRepository(session).create_for_profile(
            profile_id,
            institution="Example University",
            degree="MSc",
            field_of_study="Demo Information Technology",
            start_date=date(2022, 9, 1),
            end_date=date(2024, 1, 31),
        )
        await CertificationRepository(session).create_for_profile(
            profile_id,
            name="Demo Cloud Practitioner",
            issuer="Example Certification Body",
            issued_date=date(2025, 2, 1),
            status="active",
        )
        # Deliberately included: an in-progress certification must never be
        # rendered as one that is held.
        await CertificationRepository(session).create_for_profile(
            profile_id,
            name="Demo Solutions Architect (studying)",
            issuer="Example Certification Body",
            status="in_progress",
        )
        await SkillRepository(session).create_for_profile(
            profile_id, name="Demo Linux", category="platform", proficiency="proficient"
        )
        await SkillRepository(session).create_for_profile(
            profile_id, name="Demo Terraform", category="iac", proficiency="working"
        )
        await ProjectRepository(session).create_for_profile(
            profile_id,
            name="Demo Portfolio Project",
            summary="A fictional project used only to exercise the fact index.",
            tech_stack=["Demo Terraform", "Demo Docker"],
        )
        await session.flush()

        facts = FactRepository(session)
        draft = await facts.create_draft(
            profile_id=profile_id,
            fact_type=FactType.EXPERIENCE,
            text_value="Demo Cloud Engineer at Example Cloud Corp",
            source_table="experiences",
            source_id=experience.id,
            structured={"start_date": "2024-03-01", "numerals": []},
        )
        # One verified fact so the citable-fact path is exercisable locally.
        await facts.verify(draft.id, verified_by=user_id)

        # And one that stays draft, so "not everything is citable" is visible.
        await facts.create_draft(
            profile_id=profile_id,
            fact_type=FactType.SKILL,
            text_value="Uses Demo Terraform at a working level",
            source_table="skills",
            source_id=uuid7(),
        )

        print("seeded demo user, profile, 7 profile entries and 2 facts (1 verified)")
        print(f"  profile_id: {profile_id}")
        print(f"  user_id:    {user_id}")
        return 0


async def main() -> int:
    settings = get_settings()
    print(f"environment      : {settings.environment}")
    print(f"submission mode  : {settings.submission_mode}")

    await init_engine(settings)
    await init_redis(settings)
    try:
        db = await check_database()
        redis = await check_redis()
        revision = await get_migration_revision()
        print(f"postgres         : {db['status']}")
        print(f"redis            : {redis['status']}")
        print(f"migration        : {revision or 'not applied'}")

        if db["status"] != "ok":
            print("\ncannot seed: database unavailable")
            return 1
        if revision is None:
            print("\ncannot seed: run `make migrate` first")
            return 1
        return await seed()
    finally:
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
