"""Manual import connectors."""

from __future__ import annotations

import pytest

from packages.services.ingestion.connectors.manual import parse_csv, parse_json
from packages.shared.errors import ValidationError

pytestmark = pytest.mark.unit

CSV_OK = """title,url,company,location,salary
Cloud Engineer,https://example.invalid/j/1,Example Cloud Corp,"Dallas, TX",$120k - $150k
Senior DevOps Engineer,https://example.invalid/j/2,Example Cloud Corp,Remote,
"""


def test_parse_csv_maps_aliased_headers() -> None:
    postings = parse_csv(CSV_OK)
    assert len(postings) == 2
    assert postings[0].company_name == "Example Cloud Corp"
    assert postings[0].salary_text == "$120k - $150k"
    assert postings[1].location == "Remote"


def test_parse_csv_requires_title_and_url() -> None:
    with pytest.raises(ValidationError, match="missing required column"):
        parse_csv("company,location\nExample Cloud,Dallas\n")


def test_parse_csv_rejects_a_row_missing_a_required_value() -> None:
    """Whole-file rejection: a partial import is harder to reason about."""
    with pytest.raises(ValidationError, match="row 3"):
        parse_csv("title,url\nCloud Engineer,https://example.invalid/j/1\n,\
https://example.invalid/j/2\n")


def test_parse_csv_skips_blank_lines() -> None:
    assert len(parse_csv("title,url\nCloud Engineer,https://example.invalid/j/1\n,\n")) == 1


def test_parse_csv_rejects_an_empty_file() -> None:
    with pytest.raises(ValidationError, match="no data rows"):
        parse_csv("title,url\n")


def test_parse_csv_rejects_a_headerless_file() -> None:
    with pytest.raises(ValidationError):
        parse_csv("")


def test_parse_json_array() -> None:
    postings = parse_json(
        '[{"title": "Cloud Engineer", "url": "https://example.invalid/j/1", '
        '"company": "Example Cloud"}]'
    )
    assert postings[0].company_name == "Example Cloud"


def test_parse_json_wrapped_object() -> None:
    postings = parse_json(
        '{"jobs": [{"title": "Cloud Engineer", "url": "https://example.invalid/j/1"}]}'
    )
    assert len(postings) == 1


def test_parse_json_reports_the_syntax_error_location() -> None:
    with pytest.raises(ValidationError, match="invalid JSON"):
        parse_json("{not json")


def test_parse_json_rejects_a_bare_object() -> None:
    with pytest.raises(ValidationError, match="no array"):
        parse_json('{"title": "Cloud Engineer"}')


def test_dates_are_parsed_or_left_empty() -> None:
    postings = parse_json(
        '[{"title": "A", "url": "https://example.invalid/1", "posted_at": "2026-08-01"},'
        ' {"title": "B", "url": "https://example.invalid/2", "posted_at": "not a date"}]'
    )
    assert postings[0].posted_at is not None
    assert postings[0].posted_at.tzinfo is not None
    assert postings[1].posted_at is None


def test_no_http_connector_exists() -> None:
    """Phase 3 ships no crawler. Adding one is a ToS decision, not a coding
    convenience - this test makes that explicit rather than implicit."""
    import pkgutil

    import packages.services.ingestion.connectors as connectors

    modules = {m.name for m in pkgutil.iter_modules(connectors.__path__)}
    assert modules == {"manual"}, f"unexpected connector modules: {modules}"
