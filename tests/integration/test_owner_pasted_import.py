"""Owner-pasted job import: exempt from domain site policies, nothing else.

Approved decision (2026-11-10): a posting the signed-in owner typed or pasted
is recorded without an employer-domain site policy because JobForge makes no
request to its link. Bulk CSV/JSON import, crawling and connectors keep full
site-policy enforcement. These tests pin both halves of that boundary.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from packages.core.models import IngestionMethod, Source

pytestmark = [pytest.mark.integration, pytest.mark.safety]

DESCRIPTION = (
    "Synthetic posting for tests. Requirements: 3+ years of Linux administration, "
    "Ansible automation, Bash scripting and on-call experience."
)


def _posting(**over: str) -> dict[str, str]:
    # Company and description are unique per call as well as title and URL:
    # the test database persists between runs, and dedupe treats a similar
    # title at the same company within 120 days as the same job, so a shared
    # company name made this flaky as earlier runs' rows accumulated.
    run = uuid4().hex[:10]
    body = {
        "title": f"Synthetic Linux Administrator {run}",
        "company_name": f"Synthetic Employer {run}",
        "url": f"https://careers.unreviewed-employer.invalid/jobs/{run}",
        "description": f"{DESCRIPTION} Reference {run}.",
    }
    body.update(over)
    return body


async def test_owner_pasted_posting_is_recorded_without_a_site_policy(
    api_sessions, session
) -> None:
    owner = await api_sessions("owner")
    response = await owner.post("/api/v1/jobs/manual", json=_posting(location="Remote"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["source_slug"], body["created"], body["rejected"]) == ("owner-pasted", 1, 0)
    job_id = body["outcomes"][0]["job_id"]

    job = (await owner.get(f"/api/v1/jobs/{job_id}")).json()
    source = await session.get(Source, UUID(job["source_id"]))
    assert source is not None
    assert (source.slug, source.domain) == ("owner-pasted", "owner-pasted.jobforge.invalid")
    assert source.ingestion_method is IngestionMethod.MANUAL_IMPORT
    # Deterministic requirement extraction still ran on the pasted text.
    assert job["requirements"], job["requirements"]


async def test_the_same_pasted_posting_is_a_duplicate_not_a_second_job(api_sessions) -> None:
    owner = await api_sessions("owner")
    posting = _posting()
    first = await owner.post("/api/v1/jobs/manual", json=posting)
    second = await owner.post("/api/v1/jobs/manual", json=posting)
    assert first.json()["created"] == 1
    assert (second.json()["created"], second.json()["duplicates"]) == (0, 1)


async def test_bulk_import_of_the_same_unreviewed_domain_is_still_policy_blocked(
    api_sessions,
) -> None:
    owner = await api_sessions("owner")
    slug = f"synthetic-bulk-{uuid4().hex[:8]}"
    created = await owner.post(
        "/api/v1/sources",
        json={
            "name": "Synthetic bulk source",
            "slug": slug,
            "domain": "careers.unreviewed-employer.invalid",
            "ingestion_method": "manual_import",
        },
    )
    assert created.status_code == 201, created.text
    refused = await owner.post(
        "/api/v1/jobs/import",
        json={"source_slug": slug, "format": "json", "content": json.dumps([_posting()])},
    )
    assert refused.status_code == 403
    assert refused.json()["reason"] in {"no_policy_for_domain", "source_disabled"}


@pytest.mark.parametrize(
    "change",
    [
        {"url": "ftp://careers.example.invalid/1"},
        {"url": "https://careers.example.invalid/has space"},
        {"description": "too short"},
        {"title": ""},
        {"company_name": ""},
        {"fetch": "true"},
    ],
)
async def test_invalid_or_unexpected_fields_are_refused(api_sessions, change) -> None:
    owner = await api_sessions("owner")
    response = await owner.post("/api/v1/jobs/manual", json=_posting(**change))
    assert response.status_code == 422


async def test_only_the_owner_may_record_a_pasted_posting(app_client, api_sessions) -> None:
    assert (await app_client.post("/api/v1/jobs/manual", json=_posting())).status_code == 401
    reviewer = await api_sessions("reviewer")
    assert (await reviewer.post("/api/v1/jobs/manual", json=_posting())).status_code == 403


async def test_the_owner_pasted_source_name_is_reserved(api_sessions) -> None:
    owner = await api_sessions("owner")
    for body in (
        {"name": "x", "slug": "owner-pasted", "domain": "elsewhere.invalid"},
        {
            "name": "x",
            "slug": f"other-{uuid4().hex[:6]}",
            "domain": "owner-pasted.jobforge.invalid",
        },
    ):
        response = await owner.post("/api/v1/sources", json=body)
        assert response.status_code == 422, response.text
