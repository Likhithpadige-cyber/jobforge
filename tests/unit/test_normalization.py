"""Normalization is pure and deterministic, so it is tested exhaustively here.

Every failure mode that would cost an LLM call or produce a wrong application
is cheaper to catch at this layer than anywhere downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.services.ingestion.normalize import (
    clean_text,
    content_hash,
    extract_employment_type,
    extract_seniority,
    is_stale,
    normalize_company_name,
    normalize_job,
    normalize_title,
    parse_location,
    parse_salary,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ titles
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Cloud Engineer", "senior cloud engineer"),
        ("  Cloud   Engineer  ", "cloud engineer"),
        ("Sr. DevOps Engineer", "senior devops engineer"),
        ("Cloud Engineer (Remote)", "cloud engineer"),
        ("Cloud Engineer - Remote", "cloud engineer"),
        ("SRE", "site reliability engineer"),
        ("Cloud Engineer #REQ12345", "cloud engineer"),
        ("URGENT: Cloud Engineer - Apply Now", "cloud engineer"),
        ("Ingénieur Cloud", "ingenieur cloud"),
        ("Cloud Engineer (W2 Only)", "cloud engineer"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_normalize_title_preserves_meaningful_symbols() -> None:
    assert "c++" in normalize_title("C++ Engineer")
    assert "ci/cd" in normalize_title("CI/CD Engineer")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Cloud Engineer", "senior"),
        ("Sr Cloud Engineer", "senior"),
        ("Junior DevOps Engineer", "junior"),
        ("Jr. Infrastructure Engineer", "junior"),
        ("Staff Site Reliability Engineer", "lead"),
        ("Principal Cloud Architect", "lead"),
        ("Cloud Engineer", "unknown"),
        ("Entry-Level Cloud Engineer", "junior"),
    ],
)
def test_extract_seniority(title: str, expected: str) -> None:
    assert extract_seniority(title) == expected


def test_seniority_returns_unknown_rather_than_guessing() -> None:
    """An unlabelled title is 'unknown'. Guessing would skew the fit score."""
    assert extract_seniority("Cloud Engineer") == "unknown"
    assert extract_seniority("Infrastructure Engineer") == "unknown"


# --------------------------------------------------------------- companies
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example Cloud Corp", "example-cloud"),
        ("Example Cloud Corporation", "example-cloud"),
        ("Example Cloud, Inc.", "example-cloud"),
        ("Example Cloud LLC", "example-cloud"),
        ("Example Cloud Technologies", "example-cloud"),
        ("EXAMPLE CLOUD", "example-cloud"),
    ],
)
def test_company_variants_collapse_to_one_key(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_distinct_companies_do_not_collide() -> None:
    assert normalize_company_name("Acme Cloud") != normalize_company_name("Acme Data")


# --------------------------------------------------------------- locations
def test_location_city_and_state() -> None:
    parsed = parse_location("Fort Worth, TX")
    assert parsed.city == "Fort Worth"
    assert parsed.state == "TX"
    assert parsed.country == "US"
    assert parsed.is_remote is False


def test_location_full_state_name() -> None:
    assert parse_location("Austin, Texas").state == "TX"


def test_remote_is_detected() -> None:
    for raw in ("Remote", "Fully Remote", "100% Remote", "Remote - US", "Work From Home"):
        assert parse_location(raw).is_remote is True, raw


def test_hybrid_is_not_remote() -> None:
    """A hybrid role has an office requirement; treating it as remote would
    put jobs in the queue that the candidate cannot actually take."""
    parsed = parse_location("Hybrid - Dallas, TX")
    assert parsed.is_hybrid is True
    assert parsed.is_remote is False
    assert parsed.state == "TX"


def test_remote_with_city_keeps_both() -> None:
    parsed = parse_location("Remote, Dallas, TX")
    assert parsed.is_remote is True
    assert parsed.state == "TX"


def test_unparseable_location_returns_empty_not_a_guess() -> None:
    parsed = parse_location("Somewhere nice")
    assert parsed.state is None
    assert parsed.country is None


def test_empty_location() -> None:
    assert parse_location(None) == parse_location("")


# ------------------------------------------------------------------ salary
@pytest.mark.parametrize(
    ("text", "low", "high", "period"),
    [
        ("$120,000 - $150,000 per year", 120_000, 150_000, "yearly"),
        ("$120k - $150k", 120_000, 150_000, "yearly"),
        ("120k-150k annually", 120_000, 150_000, "yearly"),
        ("$60 - $75 per hour", 60, 75, "hourly"),
        ("Salary: 130,000 to 160,000 USD", 130_000, 160_000, "yearly"),
    ],
)
def test_parse_salary_ranges(text: str, low: int, high: int, period: str) -> None:
    salary = parse_salary(text)
    assert (salary.minimum, salary.maximum, salary.period) == (low, high, period)


def test_reversed_range_is_corrected() -> None:
    salary = parse_salary("$150,000 - $120,000")
    assert salary.minimum == 120_000
    assert salary.maximum == 150_000


def test_currency_detection() -> None:
    assert parse_salary("$120k - $150k").currency == "USD"
    assert parse_salary("£70,000 - £90,000").currency == "GBP"
    assert parse_salary("€80,000 - €95,000").currency == "EUR"


def test_single_figure_is_not_turned_into_a_range() -> None:
    """Inventing the other end of a range is inventing data."""
    assert parse_salary("competitive, around $130,000").minimum is None


def test_no_salary_text() -> None:
    assert parse_salary(None).minimum is None
    assert parse_salary("Competitive salary and benefits").minimum is None


def test_implausible_parse_is_rejected() -> None:
    """A phone number or a year must not become a salary."""
    assert parse_salary("call 555 - 1234").period != "yearly" or \
           parse_salary("call 555 - 1234").minimum is None


# ------------------------------------------------------------- description
def test_clean_text_strips_html() -> None:
    assert clean_text("<p>Hello</p><br/>World") == "Hello\n\nWorld"


def test_clean_text_strips_zero_width_characters() -> None:
    """Zero-width characters are a standard prompt-injection hiding place."""
    dirty = "Cloud\u200bEngineer\u200d role\ufeff"
    cleaned = clean_text(dirty)
    assert "\u200b" not in cleaned
    assert "\u200d" not in cleaned
    assert "\ufeff" not in cleaned


def test_clean_text_decodes_entities() -> None:
    assert clean_text("R&amp;D &lt;team&gt;") == "R&D <team>"


def test_clean_text_handles_none() -> None:
    assert clean_text(None) is None


# ------------------------------------------------------- employment types
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Cloud Engineer (Full-Time)", "full_time"),
        ("Cloud Engineer - Contract", "contract"),
        ("Cloud Engineering Intern", "internship"),
        ("Cloud Engineer", None),
    ],
)
def test_employment_type(title: str, expected: str | None) -> None:
    assert extract_employment_type(title) == expected


# ------------------------------------------------------------------ hashing
def test_content_hash_is_stable_across_cosmetic_differences() -> None:
    a = content_hash("Senior Cloud Engineer", "Example Cloud Corp", "<p>Build things</p>")
    b = content_hash("  senior   cloud engineer ", "Example Cloud, Inc.", "Build things")
    assert a == b


def test_content_hash_differs_on_real_differences() -> None:
    a = content_hash("Cloud Engineer", "Example Cloud", "Build things")
    b = content_hash("Cloud Engineer", "Example Cloud", "Build other things")
    assert a != b


# -------------------------------------------------------------- full pass
def test_normalize_job_end_to_end() -> None:
    job = normalize_job(
        title="Sr. Cloud Engineer (Remote) #REQ4471",
        url="https://example.invalid/jobs/4471",
        company_name="Example Cloud, Inc.",
        external_id="4471",
        location="Remote - Dallas, TX",
        description="<p>Work with AWS.</p> Pay is $130,000 - $160,000 per year.",
    )
    assert job.normalized_title == "senior cloud engineer"
    assert job.normalized_company == "example-cloud"
    assert job.seniority == "senior"
    assert job.is_remote is True
    assert job.location_state == "TX"
    assert job.salary_min == 130_000
    assert job.salary_period == "yearly"
    assert job.description_hash
    assert job.warnings == []


def test_normalize_job_records_warnings_instead_of_guessing() -> None:
    job = normalize_job(
        title="Cloud Engineer",
        url="https://example.invalid/j/1",
        location="Planet Zog",
        salary_text="somewhere around market rate",
    )
    assert job.location_city is None or job.location_state is None
    assert job.salary_min is None
    assert job.warnings


def test_normalize_job_rejects_empty_title() -> None:
    with pytest.raises(ValueError, match="title"):
        normalize_job(title="   ", url="https://example.invalid/j/1")


def test_normalize_job_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="url"):
        normalize_job(title="Cloud Engineer", url="")


def test_is_stale() -> None:
    assert is_stale(datetime.now(UTC) - timedelta(days=90)) is True
    assert is_stale(datetime.now(UTC) - timedelta(days=5)) is False
    assert is_stale(None) is False


def test_is_stale_handles_naive_datetimes() -> None:
    """Sources return naive timestamps constantly; this must not raise."""
    assert is_stale(datetime.now() - timedelta(days=90)) is True  # noqa: DTZ005
