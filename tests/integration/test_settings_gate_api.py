"""M13 Phase 2: the settings page reads both submission switches.

The persisted switch alone never makes submission live: the deployment's
`SUBMISSION_MODE` must also be `enabled`. This endpoint reports both, and
`live` only when both allow a real send.
"""

from __future__ import annotations

import pytest

from packages.core.models import User
from packages.core.services.settings import set_submission_dispatch_enabled
from packages.shared.config import get_settings
from tests.integration import test_submission_api as submission_api

client_factory = submission_api.client_factory
_clean_all = submission_api._clean_all

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine", "_clean_all"),
]


async def _gate(client_factory, user_id):
    client = await client_factory(user_id)
    async with client:
        res = await client.get("/api/v1/settings/submission-gate")
    assert res.status_code == 200, res.text
    return res.json()


async def _persist(session, demo_user, enabled: bool) -> None:
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, enabled, actor=owner)
    await session.commit()


async def test_dry_run_is_never_live_even_with_the_switch_on(
    client_factory, session, demo_user
) -> None:
    await _persist(session, demo_user, True)
    assert await _gate(client_factory, demo_user) == {
        "submission_mode": "dry_run",
        "persisted_enabled": True,
        "live": False,
    }


async def test_live_requires_both_switches(client_factory, session, demo_user, monkeypatch) -> None:
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()
    await session.commit()
    off = await _gate(client_factory, demo_user)
    assert off == {"submission_mode": "enabled", "persisted_enabled": False, "live": False}
    await _persist(session, demo_user, True)
    on = await _gate(client_factory, demo_user)
    assert on == {"submission_mode": "enabled", "persisted_enabled": True, "live": True}
