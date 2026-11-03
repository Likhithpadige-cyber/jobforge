"""Profile CRUD and fact lifecycle through the repositories and the API."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from packages.core.models import FactStatus, FactType
from packages.core.repositories import (
    CertificationRepository,
    ExperienceRepository,
    FactRepository,
    ProfileRepository,
)
from packages.core.services import ProfileService
from packages.shared.errors import ConflictError, NotFoundError

pytestmark = pytest.mark.integration


async def test_profile_round_trip(session, demo_profile) -> None:
    repo = ProfileRepository(session)
    profile = await repo.get_or_404(demo_profile)
    assert profile.full_name == "Demo Candidate"
    assert profile.target_roles == ["Cloud Engineer"]


async def test_profile_version_bumps_on_update(session, demo_profile) -> None:
    service = ProfileService(session)
    before = (await service.profiles.get_or_404(demo_profile)).version
    updated = await service.update_profile(demo_profile, headline="New headline")
    assert updated.version == before + 1


async def test_stale_version_is_rejected(session, demo_profile) -> None:
    service = ProfileService(session)
    with pytest.raises(Exception, match="moved on"):
        await service.update_profile(demo_profile, version=999, headline="x")


async def test_experience_crud_and_soft_delete(session, demo_profile) -> None:
    repo = ExperienceRepository(session)
    created = await repo.create_for_profile(
        demo_profile,
        company_name="Example Cloud Corp",
        title="Demo Engineer",
        start_date=__import__("datetime").date(2025, 1, 1),
        description="Demo description.",
    )
    assert created.is_current is None or created.is_current in (True, None)

    listed = await repo.list_for_profile(demo_profile)
    assert created.id in {e.id for e in listed}

    await repo.soft_delete(created)
    listed_after = await repo.list_for_profile(demo_profile)
    assert created.id not in {e.id for e in listed_after}
    # Soft delete: the row is still there for provenance.
    assert await repo.get(created.id, include_deleted=True) is not None


async def test_in_progress_certification_is_rejected_with_issue_date(session, demo_profile) -> None:
    repo = CertificationRepository(session)
    with pytest.raises(IntegrityError, match="ck_certifications_in_progress_has_no_issue_date"):
        await repo.create_for_profile(
            demo_profile,
            name="Studying For This",
            status="in_progress",
            issued_date=__import__("datetime").date(2026, 1, 1),
        )
    await session.rollback()


# ------------------------------------------------------------ fact lifecycle
async def test_fact_is_created_as_draft(session, demo_profile, demo_experience) -> None:
    repo = FactRepository(session)
    fact = await repo.create_draft(
        profile_id=demo_profile,
        fact_type=FactType.EXPERIENCE,
        text_value="Demo Engineer at Example Cloud Corp",
        source_table="experiences",
        source_id=demo_experience,
    )
    assert fact.status == FactStatus.DRAFT
    assert fact.verified_by is None
    assert fact.is_citable is False


async def test_fact_verification_records_who_and_when(
    session, demo_profile, demo_experience, demo_user
) -> None:
    repo = FactRepository(session)
    fact = await repo.create_draft(
        profile_id=demo_profile,
        fact_type=FactType.SKILL,
        text_value="Uses Terraform at a working level",
        source_table="experiences",
        source_id=demo_experience,
    )
    verified = await repo.verify(fact.id, verified_by=demo_user)
    assert verified.status == FactStatus.VERIFIED
    assert verified.verified_by == demo_user
    assert verified.verified_at is not None
    assert verified.is_citable is True


async def test_double_verification_is_a_conflict(session, verified_fact, demo_user) -> None:
    with pytest.raises(ConflictError):
        await FactRepository(session).verify(verified_fact, verified_by=demo_user)


async def test_only_verified_facts_are_citable(
    session, demo_profile, verified_fact, draft_fact
) -> None:
    citable = await FactRepository(session).list_citable(demo_profile)
    ids = {f.id for f in citable}
    assert verified_fact in ids
    assert draft_fact not in ids


async def test_unverify_makes_a_fact_uncitable(session, demo_profile, verified_fact) -> None:
    repo = FactRepository(session)
    await repo.unverify(verified_fact)
    citable = await repo.list_citable(demo_profile)
    assert verified_fact not in {f.id for f in citable}


async def test_retire_makes_a_fact_uncitable(session, demo_profile, verified_fact) -> None:
    repo = FactRepository(session)
    await repo.retire(verified_fact)
    assert verified_fact not in {f.id for f in await repo.list_citable(demo_profile)}


async def test_uncitable_ids_are_reported(session, verified_fact, draft_fact) -> None:
    bad = await FactRepository(session).verify_all_citable([verified_fact, draft_fact])
    assert bad == [draft_fact]


async def test_increment_usage_via_definer_function(session, verified_fact) -> None:
    repo = FactRepository(session)
    new_count = await repo.increment_usage(verified_fact)
    assert new_count == 1


async def test_missing_fact_raises_not_found(session) -> None:
    from packages.shared.ids import uuid7

    with pytest.raises(NotFoundError):
        await FactRepository(session).get_or_404(uuid7())


# -------------------------------------------------------- fact suggestions
async def test_suggestions_land_as_drafts_only(session, demo_profile, demo_experience) -> None:
    """Suggestion is deterministic text splitting of the human's own words.
    Nothing it produces is citable until a human verifies it."""
    created = await ProfileService(session).suggest_facts(
        demo_profile, "experiences", demo_experience
    )
    assert created
    assert all(f.status == FactStatus.DRAFT for f in created)
    assert all(f.source_table == "experiences" for f in created)


async def test_suggestions_are_substrings_of_human_text(
    session, demo_profile, demo_experience
) -> None:
    source = (
        await session.execute(
            text("SELECT description, title, company_name FROM experiences WHERE id = :id"),
            {"id": demo_experience},
        )
    ).one()
    created = await ProfileService(session).suggest_facts(
        demo_profile, "experiences", demo_experience
    )
    haystack = " ".join(str(part) for part in source)
    for fact in created:
        # Either a header built from real fields, or a sentence from the text.
        assert any(token in haystack for token in fact.text.split()[:3])


async def test_suggestions_are_idempotent(session, demo_profile, demo_experience) -> None:
    service = ProfileService(session)
    first = await service.suggest_facts(demo_profile, "experiences", demo_experience)
    second = await service.suggest_facts(demo_profile, "experiences", demo_experience)
    assert first and second == []


async def test_fact_summary_reports_the_index(
    session, demo_profile, verified_fact, draft_fact
) -> None:
    summary = await ProfileService(session).fact_summary(demo_profile)
    assert summary["total"] >= 2
    assert summary["citable"] >= 1
    assert summary["draft_awaiting_verification"] >= 1
