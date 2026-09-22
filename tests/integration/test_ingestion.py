"""Ingestion pipeline against a real database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from packages.core.models import JobStatus
from packages.services.ingestion import IngestionPipeline, RawPosting
from packages.shared.errors import PolicyBlockedError
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration


def posting(**kwargs: object) -> RawPosting:
    kwargs.setdefault("title", "Cloud Engineer")
    kwargs.setdefault("url", f"https://jobs.example.invalid/j/{uuid4().hex[:8]}")
    return RawPosting(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
async def permitted_source(session, demo_user):  # type: ignore[no-untyped-def]
    """A source whose domain has a reviewed, manual-import policy."""
    source_id = uuid7()
    domain = "jobs.example.invalid"
    await session.execute(
        text(
            "INSERT INTO site_policies (id, domain, ingestion_method, "
            "submission_method, requires_login, reviewed_by, reviewed_at, "
            "created_at, updated_at) VALUES (:id, :domain, 'manual_import', "
            "'human_handoff', true, :user, now(), now(), now()) "
            "ON CONFLICT (domain) DO UPDATE SET ingestion_method = 'manual_import'"
        ),
        {"id": uuid7(), "domain": domain, "user": demo_user},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, name, slug, domain, ingestion_method, "
            "enabled, created_at, updated_at) VALUES (:id, 'Demo Import', :slug, "
            ":domain, 'manual_import', true, now(), now())"
        ),
        {"id": source_id, "slug": f"demo-{uuid4().hex[:6]}", "domain": domain},
    )
    await session.flush()
    row = (
        await session.execute(text("SELECT slug FROM sources WHERE id = :id"),
                              {"id": source_id})
    ).scalar_one()
    return {"id": source_id, "slug": row, "domain": domain}


@pytest.mark.safety
async def test_unknown_domain_is_blocked(session, demo_user) -> None:
    """No site_policies row means blocked, with no exception path around it."""
    source_id = uuid7()
    slug = f"unrev-{uuid4().hex[:6]}"
    await session.execute(
        text(
            "INSERT INTO sources (id, name, slug, domain, ingestion_method, "
            "enabled, created_at, updated_at) VALUES (:id, 'Unreviewed', :slug, "
            "'unreviewed.example.invalid', 'manual_import', true, now(), now())"
        ),
        {"id": source_id, "slug": slug},
    )
    await session.flush()
    pipeline = IngestionPipeline(session)
    with pytest.raises(PolicyBlockedError, match="no site_policies row"):
        await pipeline.ingest_batch(slug, [posting()])


@pytest.mark.safety
async def test_disabled_source_is_blocked(session, permitted_source) -> None:
    await session.execute(
        text("UPDATE sources SET enabled = false WHERE id = :id"),
        {"id": permitted_source["id"]},
    )
    await session.flush()
    with pytest.raises(PolicyBlockedError, match="disabled"):
        await IngestionPipeline(session).ingest_batch(
            permitted_source["slug"], [posting()]
        )


async def test_ingest_creates_a_normalized_job(session, permitted_source) -> None:
    report = await IngestionPipeline(session).ingest_batch(
        permitted_source["slug"],
        [posting(
            title="Sr. Cloud Engineer (Remote)",
            company_name="Example Cloud, Inc.",
            location="Remote - Dallas, TX",
            description="Work with AWS. Pay $130,000 - $160,000 per year.",
        )],
    )
    assert report.created == 1
    job = await __import__(
        "packages.core.repositories", fromlist=["JobRepository"]
    ).JobRepository(session).get_or_404(report.outcomes[0].job_id)
    assert job.status == JobStatus.NORMALIZED
    assert job.normalized_title == "senior cloud engineer"
    assert job.seniority == "senior"
    assert job.is_remote is True
    assert job.salary_min == 130_000
    assert job.company_id is not None


async def test_identical_posting_is_marked_duplicate(session, permitted_source) -> None:
    pipeline = IngestionPipeline(session)
    common = {
        "title": "Cloud Engineer",
        "company_name": "Example Cloud Corp",
        "description": "Identical description for the dedupe test.",
    }
    first = await pipeline.ingest_batch(permitted_source["slug"], [posting(**common)])
    second = await pipeline.ingest_batch(permitted_source["slug"], [posting(**common)])
    assert first.created == 1
    assert second.duplicates == 1
    assert second.outcomes[0].canonical_id == first.outcomes[0].job_id


async def test_same_external_id_updates_rather_than_duplicating(
    session, permitted_source
) -> None:
    pipeline = IngestionPipeline(session)
    url = "https://jobs.example.invalid/j/stable"
    first = await pipeline.ingest_batch(
        permitted_source["slug"], [posting(url=url, external_id="REQ-1")]
    )
    second = await pipeline.ingest_batch(
        permitted_source["slug"], [posting(url=url, external_id="REQ-1")]
    )
    assert first.created == 1
    assert second.updated == 1
    assert second.outcomes[0].job_id == first.outcomes[0].job_id


async def test_different_roles_are_both_kept(session, permitted_source) -> None:
    pipeline = IngestionPipeline(session)
    report = await pipeline.ingest_batch(
        permitted_source["slug"],
        [
            posting(title="Junior Cloud Engineer", company_name="Example Cloud Corp",
                    description="Junior role."),
            posting(title="Senior Cloud Engineer", company_name="Example Cloud Corp",
                    description="Senior role."),
        ],
    )
    assert report.created == 2, "a junior and a senior opening are two real jobs"


async def test_stale_posting_is_rejected(session, permitted_source) -> None:
    report = await IngestionPipeline(session).ingest_batch(
        permitted_source["slug"],
        [posting(posted_at=datetime.now(UTC) - timedelta(days=200))],
    )
    assert report.rejected == 1
    assert "older than" in report.outcomes[0].reason


async def test_posting_on_an_unreviewed_domain_is_rejected(
    session, permitted_source
) -> None:
    """A source may legally list jobs whose apply URLs point elsewhere.
    Those still need their own reviewed policy."""
    report = await IngestionPipeline(session).ingest_batch(
        permitted_source["slug"],
        [posting(url="https://elsewhere.example.invalid/j/1")],
    )
    assert report.rejected == 1
    assert "not permitted" in report.outcomes[0].reason


async def test_malformed_posting_is_rejected_not_crashed(
    session, permitted_source
) -> None:
    report = await IngestionPipeline(session).ingest_batch(
        permitted_source["slug"],
        [posting(title="   "), posting(title="Cloud Engineer")],
    )
    assert report.rejected == 1
    assert report.created == 1


async def test_company_variants_resolve_to_one_row(session, permitted_source) -> None:
    pipeline = IngestionPipeline(session)
    await pipeline.ingest_batch(
        permitted_source["slug"],
        [
            posting(title="Cloud Engineer", company_name="Example Cloud Corp",
                    description="A"),
            posting(title="Data Engineer", company_name="Example Cloud, Inc.",
                    description="B"),
        ],
    )
    count = (
        await session.execute(
            text("SELECT count(*) FROM companies WHERE normalized_name = 'example-cloud'")
        )
    ).scalar_one()
    assert count == 1


async def test_duplicate_does_not_reach_normalized_status(
    session, permitted_source
) -> None:
    """Only `normalized` jobs are picked up for evaluation, so a duplicate
    must never carry that status - otherwise it costs an LLM call."""
    pipeline = IngestionPipeline(session)
    common = {"title": "Cloud Engineer", "company_name": "Example Cloud Corp",
              "description": "Same text."}
    await pipeline.ingest_batch(permitted_source["slug"], [posting(**common)])
    second = await pipeline.ingest_batch(permitted_source["slug"], [posting(**common)])
    job_id = second.outcomes[0].job_id
    status = (
        await session.execute(text("SELECT status FROM jobs WHERE id = :id"),
                              {"id": job_id})
    ).scalar_one()
    assert status == "duplicate"
