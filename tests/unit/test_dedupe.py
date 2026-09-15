"""Duplicate detection.

Two failure modes matter and they pull in opposite directions:
a missed duplicate wastes an LLM call and risks a second application to the
same role; a false duplicate means never applying to a genuine second opening.
The threshold is tuned toward the second being worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.services.ingestion.dedupe import (
    DuplicateReason,
    canonical_url,
    choose_canonical,
    find_duplicate,
    title_similarity,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeJob:
    id: UUID
    url: str
    normalized_title: str | None = None
    description_hash: str | None = None
    external_id: str | None = None
    company_id: UUID | None = None
    posted_at: datetime | None = None


def job(**kwargs: object) -> FakeJob:
    kwargs.setdefault("id", uuid4())
    kwargs.setdefault("url", f"https://example.invalid/j/{uuid4().hex[:6]}")
    return FakeJob(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------ canonical URL
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.invalid/jobs/1?utm_source=linkedin",
         "https://example.invalid/jobs/1"),
        ("https://www.example.invalid/jobs/1", "https://example.invalid/jobs/1"),
        ("https://example.invalid/jobs/1/", "https://example.invalid/jobs/1"),
        ("https://example.invalid/jobs/1#apply", "https://example.invalid/jobs/1"),
        ("HTTPS://EXAMPLE.INVALID/jobs/1", "https://example.invalid/jobs/1"),
    ],
)
def test_canonical_url_strips_noise(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_canonical_url_keeps_meaningful_query_params() -> None:
    """The job id often lives in the query string; stripping it would merge
    every posting on the board into one."""
    assert "id=4471" in canonical_url("https://example.invalid/view?id=4471&utm_source=x")


# ------------------------------------------------------------- similarity
def test_identical_titles() -> None:
    assert title_similarity("cloud engineer", "cloud engineer") == 1.0


def test_reordered_titles_are_similar() -> None:
    assert title_similarity("senior cloud engineer", "cloud engineer senior") == 1.0


def test_different_roles_are_dissimilar() -> None:
    assert title_similarity("cloud engineer", "marketing manager") < 0.2


def test_seniority_difference_lowers_similarity() -> None:
    score = title_similarity("senior cloud engineer", "junior cloud engineer")
    assert 0.3 < score < 0.82, score


def test_empty_titles() -> None:
    assert title_similarity("", "cloud engineer") == 0.0


# --------------------------------------------------------------- detection
def test_exact_content_hash_is_a_duplicate() -> None:
    existing = job(description_hash="abc123")
    candidate = job(description_hash="abc123")
    match = find_duplicate(candidate, [existing])
    assert match is not None
    assert match.reason == DuplicateReason.EXACT_CONTENT
    assert match.canonical_id == existing.id


def test_same_external_id_is_a_duplicate() -> None:
    existing = job(external_id="REQ-4471")
    candidate = job(external_id="REQ-4471")
    match = find_duplicate(candidate, [existing])
    assert match is not None
    assert match.reason == DuplicateReason.SAME_SOURCE_ID


def test_same_url_after_tracking_removal_is_a_duplicate() -> None:
    existing = job(url="https://example.invalid/jobs/9")
    candidate = job(url="https://www.example.invalid/jobs/9/?utm_campaign=spring")
    match = find_duplicate(candidate, [existing])
    assert match is not None
    assert match.reason == DuplicateReason.SAME_URL


def test_same_role_at_same_company_is_a_duplicate() -> None:
    company = uuid4()
    existing = job(company_id=company, normalized_title="senior cloud engineer aws")
    candidate = job(company_id=company, normalized_title="senior cloud engineer aws")
    match = find_duplicate(candidate, [existing])
    assert match is not None
    assert match.reason == DuplicateReason.SAME_ROLE_AT_COMPANY
    assert match.similarity >= 0.82


def test_same_title_at_different_companies_is_not_a_duplicate() -> None:
    """The same title at two employers is two real jobs."""
    existing = job(company_id=uuid4(), normalized_title="cloud engineer")
    candidate = job(company_id=uuid4(), normalized_title="cloud engineer")
    assert find_duplicate(candidate, [existing]) is None


def test_different_seniority_at_same_company_is_not_a_duplicate() -> None:
    """A junior and a senior opening are two applications, not one."""
    company = uuid4()
    existing = job(company_id=company, normalized_title="junior cloud engineer")
    candidate = job(company_id=company, normalized_title="senior cloud engineer")
    assert find_duplicate(candidate, [existing]) is None


def test_no_candidates_means_no_duplicate() -> None:
    assert find_duplicate(job(description_hash="x"), []) is None


def test_a_job_does_not_duplicate_itself() -> None:
    existing = job(description_hash="abc")
    assert find_duplicate(existing, [existing]) is None


def test_first_strategy_wins() -> None:
    """Exact content beats a fuzzy title match, so the note is accurate."""
    company = uuid4()
    fuzzy = job(company_id=company, normalized_title="cloud engineer")
    exact = job(description_hash="same")
    candidate = job(
        company_id=company, normalized_title="cloud engineer", description_hash="same"
    )
    match = find_duplicate(candidate, [fuzzy, exact])
    assert match is not None
    assert match.reason == DuplicateReason.EXACT_CONTENT


# --------------------------------------------------------------- canonical
def test_posting_with_a_date_wins() -> None:
    dated = job(posted_at=datetime(2026, 1, 1, tzinfo=UTC))
    undated = job()
    canonical, duplicate = choose_canonical(undated, dated)
    assert canonical is dated
    assert duplicate is undated


def test_older_posting_wins() -> None:
    older = job(posted_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = job(posted_at=datetime(2026, 2, 1, tzinfo=UTC))
    canonical, _ = choose_canonical(newer, older)
    assert canonical is older


def test_external_id_breaks_the_tie() -> None:
    with_id = job(external_id="R1")
    without = job()
    canonical, _ = choose_canonical(without, with_id)
    assert canonical is with_id
