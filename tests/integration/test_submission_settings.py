"""M11 Phase 1: the persisted submission-dispatch switch and the two-switch
safety gate, against a real database and the real API.

Nothing here submits anything - Phase 1 builds only the switch, the gate,
and their authorization boundary. `POST /submit` does not exist yet.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models import Setting, User
from packages.core.services.settings import (
    SUBMISSION_ENABLED_KEY,
    set_submission_dispatch_enabled,
    submission_dispatch_enabled,
)
from packages.core.services.submission_gate import real_submission_enabled
from packages.shared.config import Settings, SubmissionMode

pytestmark = [pytest.mark.integration, pytest.mark.safety]


@pytest_asyncio.fixture(autouse=True)
async def _clean_submission_enabled_row(engine):  # type: ignore[no-untyped-def]
    """`settings` is genuinely global, shared state - unlike almost every
    other fixture in this suite, a row here is not scoped to one test's own
    application/job/profile ids, so a test that commits through the real API
    (as several below do, on purpose, to prove the HTTP-level authorization
    boundary) would otherwise leak `submission_enabled` into every test that
    runs after it, in this file or any other. Delete it before and after
    every test in this file, on its own connection, independent of the
    rolled-back `session` fixture."""
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _delete() -> None:
        async with maker() as conn:
            await conn.execute(
                text("DELETE FROM settings WHERE key = :key"), {"key": SUBMISSION_ENABLED_KEY}
            )
            await conn.commit()

    await _delete()
    yield
    await _delete()


# --------------------------------------------------------- service layer
async def test_submission_enabled_absent_row_is_false(session) -> None:  # type: ignore[no-untyped-def]
    assert await submission_dispatch_enabled(session) is False


async def test_submission_enabled_false_after_explicit_disable(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    await set_submission_dispatch_enabled(session, False, actor=owner)
    assert await submission_dispatch_enabled(session) is False


async def test_submission_enabled_true_after_explicit_enable(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    assert await submission_dispatch_enabled(session) is True


async def test_set_creates_exactly_one_row_and_records_the_actor(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    rows = (
        (await session.execute(select(Setting).where(Setting.key == SUBMISSION_ENABLED_KEY)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].value == {"enabled": True}
    assert rows[0].updated_by == owner.id


async def test_set_updates_the_same_row_not_a_second_one(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    await set_submission_dispatch_enabled(session, False, actor=owner)
    rows = (
        (await session.execute(select(Setting).where(Setting.key == SUBMISSION_ENABLED_KEY)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].value == {"enabled": False}


# ------------------------------------------------------------- the gate
async def test_gate_requires_both_switches_env_alone_is_insufficient(session) -> None:  # type: ignore[no-untyped-def]
    """`SUBMISSION_MODE=enabled` alone, DB switch untouched (absent = false),
    must not enable real dispatch."""
    settings = Settings(submission_mode=SubmissionMode.ENABLED)
    assert await real_submission_enabled(session, settings) is False


async def test_gate_requires_both_switches_db_alone_is_insufficient(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    """The persisted switch alone, with `SUBMISSION_MODE` left at its safe
    default (dry_run), must not enable real dispatch."""
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    settings = Settings(submission_mode=SubmissionMode.DRY_RUN)
    assert await real_submission_enabled(session, settings) is False


async def test_gate_both_switches_true_enables_the_gate_only(
    session,
    demo_user,  # type: ignore[no-untyped-def]
) -> None:
    """Both true produces an internally *enabled* gate - it still causes no
    external submission by itself: nothing in Phase 1 calls this gate from
    any code path that sends anything. There is no `POST /submit`, no
    `submission_ready -> submitting` edge, and no code that dispatches an
    email anywhere in this application. The gate being true only means a
    later phase's own dispatch check, once it exists, would pass it."""
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    settings = Settings(submission_mode=SubmissionMode.ENABLED)
    assert await real_submission_enabled(session, settings) is True


async def test_gate_default_settings_and_default_switch_is_disabled(session) -> None:  # type: ignore[no-untyped-def]
    """Both defaults (dry_run, absent row) compose to False, not an error."""
    assert await real_submission_enabled(session, Settings()) is False


# --------------------------------------------------------------- API layer
async def test_get_returns_false_before_any_row_exists(api_sessions) -> None:  # type: ignore[no-untyped-def]
    owner = await api_sessions("owner")
    response = await owner.get("/api/v1/settings/submission-enabled")
    assert response.status_code == 200
    assert response.json() == {"submission_enabled": False}


async def test_owner_can_change_the_switch(api_sessions) -> None:  # type: ignore[no-untyped-def]
    owner = await api_sessions("owner")
    put = await owner.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    assert put.status_code == 200
    assert put.json() == {"submission_enabled": True}
    get = await owner.get("/api/v1/settings/submission-enabled")
    assert get.json() == {"submission_enabled": True}


async def test_reviewer_cannot_change_the_switch(api_sessions) -> None:  # type: ignore[no-untyped-def]
    reviewer = await api_sessions("reviewer")
    response = await reviewer.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    assert response.status_code == 403


async def test_reviewer_cannot_change_the_switch_even_when_already_true(
    api_sessions,  # type: ignore[no-untyped-def]
) -> None:
    """A reviewer refused while disabled might merely mean an unrelated
    404/pre-check short-circuit; prove the refusal holds starting from the
    enabled state too."""
    owner = await api_sessions("owner")
    await owner.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    reviewer = await api_sessions("reviewer")
    response = await reviewer.put("/api/v1/settings/submission-enabled", json={"enabled": False})
    assert response.status_code == 403
    still_true = await owner.get("/api/v1/settings/submission-enabled")
    assert still_true.json() == {"submission_enabled": True}


async def test_owner_and_reviewer_can_both_read(api_sessions) -> None:  # type: ignore[no-untyped-def]
    owner = await api_sessions("owner")
    reviewer = await api_sessions("reviewer")
    await owner.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    owner_read = await owner.get("/api/v1/settings/submission-enabled")
    reviewer_read = await reviewer.get("/api/v1/settings/submission-enabled")
    assert owner_read.json() == reviewer_read.json() == {"submission_enabled": True}


async def test_unauthenticated_request_is_refused(app_client) -> None:  # type: ignore[no-untyped-def]
    get = await app_client.get("/api/v1/settings/submission-enabled")
    put = await app_client.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    assert get.status_code == 401
    assert put.status_code == 401


async def test_put_rejects_unknown_fields(api_sessions) -> None:  # type: ignore[no-untyped-def]
    owner = await api_sessions("owner")
    response = await owner.put(
        "/api/v1/settings/submission-enabled",
        json={"enabled": True, "submission_mode": "enabled"},
    )
    assert response.status_code == 422
