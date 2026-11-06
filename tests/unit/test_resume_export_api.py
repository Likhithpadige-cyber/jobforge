"""New export endpoints with real ASGI routing/CSRF/roles and synthetic stores."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest

from apps.api import deps
from apps.api.main import create_app
from packages.core.auth.sessions import Session
from packages.core.db.session import get_session
from packages.core.models import User
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError

pytestmark = [pytest.mark.unit, pytest.mark.safety]
APP_ID = UUID(int=8)


@pytest.fixture
def isolated_api():
    app = create_app()
    actor = User(id=UUID(int=9), role="owner", email="candidate@example.invalid")
    now = utc_now()
    session = Session(
        "synthetic-session", actor.id, "synthetic-csrf", now, now, now + timedelta(hours=1)
    )
    db = AsyncMock()

    async def db_override():
        yield db

    app.dependency_overrides[get_session] = db_override
    app.dependency_overrides[deps.current_session] = lambda: session
    app.dependency_overrides[deps.current_user] = lambda: actor
    # Rate limiting is tested elsewhere; no Redis/network is used here.
    for route in app.routes:
        if hasattr(route, "dependant"):
            for dependency in route.dependant.dependencies:
                if getattr(
                    dependency.call, "__name__", ""
                ) == "dependency" and "rate_limited" in getattr(
                    dependency.call, "__qualname__", ""
                ):
                    app.dependency_overrides[dependency.call] = lambda: None
    return app, actor, db


@pytest.mark.asyncio
async def test_owner_preview_is_private_and_reviewers_are_refused(isolated_api):
    app, actor, db = isolated_api
    resume = SimpleNamespace(id=UUID(int=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with (
            patch(
                "packages.core.services.resume_review.latest_tailored_resume",
                AsyncMock(return_value=resume),
            ),
            patch(
                "packages.core.services.resume_export.preview_export",
                AsyncMock(
                    return_value={
                        "digest": "a" * 64,
                        "document": {"contact": {"name": "Synthetic Candidate"}},
                    }
                ),
            ) as preview,
        ):
            response = await client.get(
                f"/api/v1/applications/{APP_ID}/resume-review/export-preview"
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            actor.role = "reviewer"
            denied = await client.get(f"/api/v1/applications/{APP_ID}/resume-review/export-preview")
            assert denied.status_code == 403
            assert preview.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_package_requires_csrf_and_owner_before_building(isolated_api):
    app, actor, _ = isolated_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        no_csrf = await client.post(f"/api/v1/applications/{APP_ID}/manual-packet")
        assert no_csrf.status_code == 403
        actor.role = "reviewer"
        denied = await client.post(
            f"/api/v1/applications/{APP_ID}/manual-packet",
            headers={"X-CSRF-Token": "synthetic-csrf"},
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_stale_export_approval_is_409_and_never_commits(isolated_api):
    app, _, db = isolated_api
    with patch(
        "packages.core.services.resume_review.approve",
        AsyncMock(side_effect=ConflictError("Export details changed")),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/applications/{APP_ID}/resume-review/approve",
                headers={"X-CSRF-Token": "synthetic-csrf"},
                json={"export_digest": "a" * 64},
            )
    assert response.status_code == 409
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,owns,expected", [("owner", True, 200), ("owner", False, 404), ("reviewer", True, 404)]
)
async def test_contact_export_download_checks_profile_owner_before_storage(
    isolated_api, role, owns, expected
):
    import hashlib

    from packages.core.models import Artifact, Profile

    app, actor, db = isolated_api
    actor.role = role
    raw = b"synthetic-pdf-bytes"
    artifact = Artifact(
        id=UUID(int=20),
        resume_id=UUID(int=1),
        template_version="ats-single-column/2",
        content_type="application/pdf",
        sha256=hashlib.sha256(raw).hexdigest(),
        storage_key="synthetic.pdf",
        kind="resume_pdf",
        resume_version=3,
        application_id=APP_ID,
    )
    from tests.unit.test_resume_export import sample, snapshot

    resume, export_data = sample()
    # This authorization fixture has no credential/source records.
    export_data["records"] = []
    resume.content = {}
    artifact.approval_id = UUID(int=7)
    approval = SimpleNamespace(
        subject_snapshot=snapshot(resume, export_data), status="approved", decided_at=None
    )
    profile = Profile(id=UUID(int=2), user_id=actor.id if owns else UUID(int=10))
    db.get.side_effect = [artifact, approval, resume, profile]
    store = AsyncMock()
    store.get.return_value = raw
    with patch("apps.api.routers.artifacts.artifact_store", return_value=store) as factory:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/v1/artifacts/{artifact.id}/content")
    assert response.status_code == expected
    if expected == 200:
        assert response.content == raw
        assert response.headers["cache-control"] == "private, no-store"
    else:
        factory.assert_not_called()
