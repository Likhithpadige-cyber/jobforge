"""Sign out everywhere (M22 prerequisite) through the real API."""

from __future__ import annotations

import pytest

from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api

pytestmark = [pytest.mark.integration, pytest.mark.safety]

client_factory = submission_api.client_factory


async def test_logout_everywhere_revokes_every_device_of_that_user_only(
    client_factory, session, demo_user
) -> None:
    from packages.core.models import User

    other = User(id=uuid7(), email=f"reviewer-{uuid7().hex}@example.invalid", role="reviewer")
    session.add(other)
    await session.commit()

    phone = await client_factory(demo_user)
    laptop = await client_factory(demo_user)
    colleague = await client_factory(other.id)
    async with phone, laptop, colleague:
        for client in (phone, laptop, colleague):
            assert (await client.get("/api/v1/auth/me")).status_code == 200

        response = await laptop.post("/api/v1/auth/logout-everywhere")
        assert response.status_code == 200 and response.json() == {"revoked": True}
        # The cookies on the calling device are cleared.
        assert any(
            "Max-Age=0" in h or "expires=" in h.lower()
            for h in response.headers.get_list("set-cookie")
        )

        assert (await phone.get("/api/v1/auth/me")).status_code == 401
        assert (await laptop.get("/api/v1/auth/me")).status_code == 401
        assert (await colleague.get("/api/v1/auth/me")).status_code == 200

    # A fresh sign-in afterwards works normally.
    async with await client_factory(demo_user) as again:
        assert (await again.get("/api/v1/auth/me")).status_code == 200


async def test_logout_everywhere_requires_csrf_and_a_session(
    client_factory, demo_user, session
) -> None:
    await session.commit()
    async with await client_factory(demo_user, csrf=False) as no_csrf:
        assert (await no_csrf.post("/api/v1/auth/logout-everywhere")).status_code == 403
        assert (await no_csrf.get("/api/v1/auth/me")).status_code == 200  # nothing revoked
    async with await client_factory(None) as anonymous:
        assert (await anonymous.post("/api/v1/auth/logout-everywhere")).status_code in (401, 403)
