"""M-A source validation/owner-authorized replacement, never an evidence rewrite."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from apps.api.routers import facts as route
from apps.api.schemas.facts import FactCreate
from packages.core.models import Experience, FactEvidence, FactStatus, FactType, User
from packages.core.repositories import FactRepository
from packages.core.services.profile_service import ProfileService
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]


async def owner(session, demo_user):
    return await session.get(User, demo_user)


@pytest.mark.parametrize(
    "source", ["missing", "foreign", "deleted", "valid", "profile", "foreign_profile"]
)
async def test_create_fact_validates_source_and_profile_owner(
    session, demo_profile, demo_experience, demo_user, source
):
    actor = await owner(session, demo_user)
    source_table = "experiences"
    source_id = demo_experience
    if source == "missing":
        source_id = uuid7()
    elif source == "deleted":
        exp = await session.get(Experience, source_id)
        exp.deleted_at = utc_now()
        await session.flush()
    elif source == "foreign":
        from packages.core.models import Profile

        other_user = User(id=uuid7(), role="owner", email="other-source@example.invalid")
        session.add(other_user)
        await session.flush()
        other = Profile(id=uuid7(), user_id=other_user.id, full_name="Other fictional profile")
        session.add(other)
        await session.flush()
        exp = Experience(
            id=uuid7(),
            profile_id=other.id,
            title="Fictional Role",
            company_name="Example",
            start_date=__import__("datetime").date(2020, 1, 1),
        )
        session.add(exp)
        await session.flush()
        source_id = exp.id
    elif source in {"profile", "foreign_profile"}:
        source_table = "profiles"
        source_id = demo_profile if source == "profile" else uuid7()
    payload = FactCreate(
        fact_type=FactType.EXPERIENCE,
        text="Synthetic source validation fact",
        source_table=source_table,
        source_id=source_id,
    )
    with (
        patch.object(route, "_profile_id", AsyncMock(return_value=demo_profile)),
        patch.object(route, "_saved", AsyncMock(side_effect=lambda session, fact: fact)) as saved,
    ):
        if source in {"valid", "profile"}:
            fact = await route.create_fact(payload, session, actor)
            assert fact.status == FactStatus.DRAFT and fact.source_id == source_id
        else:
            with pytest.raises(ValidationError, match="existing, active"):
                await route.create_fact(payload, session, actor)
            saved.assert_not_called()


async def dangling(session, profile_id, user_id):
    repo = FactRepository(session)
    fact = await repo.create_draft(
        profile_id=profile_id,
        fact_type=FactType.EXPERIENCE,
        text_value="Synthetic historical claim",
        source_table="experiences",
        source_id=uuid7(),
        structured={"count": 3},
    )
    await repo.verify(fact.id, verified_by=user_id)
    await session.refresh(fact)
    return fact


async def test_repair_preserves_original_and_creates_attributed_unverified_replacement(
    session, demo_profile, demo_experience, demo_user
):
    fact = await dangling(session, demo_profile, demo_user)
    original = (fact.id, fact.text, fact.source_table, fact.source_id, fact.structured.copy())
    expected = fact.updated_at
    replacement = await ProfileService(session).repair_fact_source(
        profile_id=demo_profile,
        fact_id=fact.id,
        source_id=demo_experience,
        expected_updated_at=expected,
        note="Owner selected the correct synthetic employment record",
        actor=await owner(session, demo_user),
    )
    assert replacement.id != fact.id and replacement.source_id == demo_experience
    assert replacement.status == FactStatus.DRAFT and replacement.verified_at is None
    assert fact.status == FactStatus.RETIRED
    assert original == (fact.id, fact.text, fact.source_table, fact.source_id, fact.structured)
    history = list(
        (
            await session.scalars(
                select(FactEvidence).where(FactEvidence.fact_id.in_([fact.id, replacement.id]))
            )
        ).all()
    )
    assert len(history) == 2
    assert all(
        e.added_by == demo_user and str(fact.id) in e.note and str(replacement.id) in e.note
        for e in history
    )
    await session.refresh(fact)
    with pytest.raises(ConflictError):
        await ProfileService(session).repair_fact_source(
            profile_id=demo_profile,
            fact_id=fact.id,
            source_id=demo_experience,
            expected_updated_at=expected,
            note="Repeated request",
            actor=await owner(session, demo_user),
        )


@pytest.mark.parametrize(
    "bad",
    [
        "stale",
        "missing_target",
        "foreign_owner",
        "reviewer",
        "empty_note",
        "valid_original",
        "duplicate",
    ],
)
async def test_repair_refuses_unsafe_request_without_rewriting_history(
    session, demo_profile, demo_experience, demo_user, bad
):
    fact = await dangling(session, demo_profile, demo_user)
    repo = FactRepository(session)
    expected = fact.updated_at
    target = demo_experience
    actor = await owner(session, demo_user)
    note = "Synthetic owner repair"
    if bad == "stale":
        expected -= timedelta(seconds=1)
    elif bad == "missing_target":
        target = uuid7()
    elif bad in {"foreign_owner", "reviewer"}:
        actor = User(
            id=uuid7(),
            role="reviewer" if bad == "reviewer" else "owner",
            email="other-owner@example.invalid",
        )
    elif bad == "empty_note":
        note = " "
    elif bad == "valid_original":
        # Only draft substance can change; use the supported lifecycle.
        await repo.unverify(fact.id)
        fact.source_id = demo_experience
        await session.flush()
        await session.refresh(fact)
        expected = fact.updated_at
    elif bad == "duplicate":
        await repo.create_draft(
            profile_id=demo_profile,
            fact_type=FactType.EXPERIENCE,
            text_value=fact.text,
            source_table="experiences",
            source_id=demo_experience,
        )
    original = (fact.text, fact.source_id, fact.status)
    with pytest.raises((ConflictError, NotFoundError, ValidationError)):
        await ProfileService(session).repair_fact_source(
            profile_id=demo_profile,
            fact_id=fact.id,
            source_id=target,
            expected_updated_at=expected,
            note=note,
            actor=actor,
        )
    assert original == (fact.text, fact.source_id, fact.status)
    assert not list(
        (await session.scalars(select(FactEvidence).where(FactEvidence.fact_id == fact.id))).all()
    )
