"""M13 Phase 2: human attribution on facts comes from the session.

Verifying a fact and attaching evidence are attributable human actions. The
person recorded is always the authenticated owner; a request body naming
anyone else is refused, and omitting the field is the normal case.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core.models import User
from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api

client_factory = submission_api.client_factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]


async def _draft(session, demo_profile, demo_experience) -> str:
    fact_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, structured, "
            "source_table, source_id, status, created_at, updated_at) VALUES "
            "(:id, :p, 'skill', 'Uses Terraform daily', '{}'::jsonb, 'experiences', :s, "
            "'draft', now(), now())"
        ),
        {"id": fact_id, "p": demo_profile, "s": demo_experience},
    )
    await session.commit()
    return str(fact_id)


async def test_verification_is_attributed_to_the_signed_in_owner(
    client_factory, session, demo_profile, demo_experience, demo_user
) -> None:
    fact_id = await _draft(session, demo_profile, demo_experience)
    client = await client_factory(demo_user)
    async with client:
        res = await client.post(f"/api/v1/profile/facts/{fact_id}/verify", json={})
    assert res.status_code == 200, res.text
    assert res.json()["verified_by"] == str(demo_user)
    assert res.json()["status"] == "verified"


async def test_verification_cannot_name_someone_else(
    client_factory, session, demo_profile, demo_experience, demo_user
) -> None:
    fact_id = await _draft(session, demo_profile, demo_experience)
    client = await client_factory(demo_user)
    async with client:
        res = await client.post(
            f"/api/v1/profile/facts/{fact_id}/verify", json={"verified_by": str(uuid7())}
        )
        assert res.status_code == 422, res.text
        same = await client.post(
            f"/api/v1/profile/facts/{fact_id}/verify", json={"verified_by": str(demo_user)}
        )
    assert same.status_code == 200, same.text
    status = await session.scalar(
        text("SELECT status FROM profile_facts WHERE id = :id"), {"id": fact_id}
    )
    assert status == "verified"


async def test_evidence_is_attributed_to_the_signed_in_owner(
    client_factory, session, demo_profile, demo_experience, demo_user
) -> None:
    fact_id = await _draft(session, demo_profile, demo_experience)
    client = await client_factory(demo_user)
    async with client:
        forged = await client.post(
            f"/api/v1/profile/facts/{fact_id}/evidence",
            json={"kind": "attestation", "note": "Synthetic", "added_by": str(uuid7())},
        )
        assert forged.status_code == 422, forged.text
        res = await client.post(
            f"/api/v1/profile/facts/{fact_id}/evidence",
            json={"kind": "attestation", "note": "Synthetic note"},
        )
    assert res.status_code == 201, res.text
    assert res.json()["added_by"] == str(demo_user)


async def test_fact_lifecycle_through_the_api(
    client_factory, session, demo_profile, demo_experience, demo_user
) -> None:
    """Every state change returns the complete, fresh record."""
    fact_id = await _draft(session, demo_profile, demo_experience)
    base = f"/api/v1/profile/facts/{fact_id}"
    client = await client_factory(demo_user)
    async with client:
        edited = await client.patch(base, json={"text": "Uses Terraform every day"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["text"] == "Uses Terraform every day"
        verified = await client.post(f"{base}/verify", json={})
        assert verified.status_code == 200, verified.text
        refused = await client.patch(base, json={"text": "Changed in place"})
        assert refused.status_code == 409, refused.text
        unverified = await client.post(f"{base}/unverify")
        assert unverified.status_code == 200, unverified.text
        assert unverified.json()["status"] == "draft"
        assert unverified.json()["verified_by"] is None
        retired = await client.post(f"{base}/retire")
        assert retired.status_code == 200, retired.text
        assert retired.json()["status"] == "retired"
        assert retired.json()["updated_at"] >= edited.json()["updated_at"]


async def test_profile_section_update_returns_the_record(
    client_factory, session, demo_profile, demo_experience, demo_user
) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        res = await client.patch(
            f"/api/v1/profile/experiences/{demo_experience}", json={"title": "Platform Engineer"}
        )
        profile = await client.put(
            "/api/v1/profile",
            json={"full_name": "Demo Candidate", "headline": "Synthetic headline"},
        )
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "Platform Engineer"
    assert profile.status_code == 200, profile.text
    assert profile.json()["headline"] == "Synthetic headline"


async def test_reviewers_never_receive_private_profile_fields(
    client_factory, session, demo_profile, demo_user
) -> None:
    await session.commit()
    async with await client_factory(demo_user) as owner:
        # The API serves the singleton profile, which in a shared test
        # database need not be this test's fixture row.
        served = (await owner.get("/api/v1/profile")).json()["id"]
    await session.execute(
        text(
            "UPDATE profiles SET email = 'private@example.invalid', phone = '555-0100', "
            "work_authorization = 'SYNTHETIC-AUTH', min_salary = 1, max_salary = 2 WHERE id = :id"
        ),
        {"id": served},
    )
    reviewer = User(id=uuid7(), email=f"reviewer-{uuid7().hex}@example.invalid", role="reviewer")
    session.add(reviewer)
    await session.commit()

    owner = await client_factory(demo_user)
    async with owner:
        full = await owner.get("/api/v1/profile")
    assert full.status_code == 200, full.text
    assert full.json()["work_authorization"] == "SYNTHETIC-AUTH"

    client = await client_factory(reviewer.id)
    async with client:
        res = await client.get("/api/v1/profile")
    assert res.status_code == 200, res.text
    body = res.json()
    for field in ("email", "phone", "work_authorization", "min_salary", "max_salary"):
        assert body[field] is None, field
    assert "private@example.invalid" not in res.text and "SYNTHETIC-AUTH" not in res.text
    assert body["full_name"] == full.json()["full_name"]
    assert body["experiences"] == full.json()["experiences"]
