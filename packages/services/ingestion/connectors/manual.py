"""Manual import connectors.

Phase 3 ships CSV and JSON import only. There is no HTTP connector, no
crawler and no browser automation in this package, and that is deliberate:
those paths need a reviewed `site_policies` row per domain plus a permitted
`ingestion_method`, and none exists yet. Adding one is a Phase 7 decision
with a ToS review attached, not a coding convenience.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from packages.shared.clock import ensure_utc
from packages.services.ingestion.types import RawPosting
from packages.shared.errors import ValidationError

REQUIRED_COLUMNS = ("title", "url")
KNOWN_COLUMNS = (
    "title", "url", "company_name", "external_id", "location",
    "description", "salary_text", "posted_at", "expires_at",
)

#: Common header spellings mapped onto our field names.
COLUMN_ALIASES = {
    "job_title": "title", "position": "title", "role": "title",
    "job_url": "url", "link": "url", "apply_url": "url",
    "company": "company_name", "employer": "company_name", "organisation": "company_name",
    "organization": "company_name", "job_id": "external_id", "id": "external_id",
    "req_id": "external_id", "requisition_id": "external_id",
    "city": "location", "job_location": "location",
    "job_description": "description", "summary": "description",
    "salary": "salary_text", "compensation": "salary_text", "pay": "salary_text",
    "date_posted": "posted_at", "posted": "posted_at", "published_at": "posted_at",
}

MAX_ROWS = 5_000


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse a timestamp, or return None. Never guesses a date."""
    if not value or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for parser in (
        lambda v: datetime.fromisoformat(v),
        lambda v: datetime.strptime(v, "%Y-%m-%d"),  # noqa: DTZ007
        lambda v: datetime.strptime(v, "%m/%d/%Y"),  # noqa: DTZ007
        lambda v: datetime.strptime(v, "%d/%m/%Y"),  # noqa: DTZ007
    ):
        try:
            parsed = parser(text)
        except (ValueError, TypeError):
            continue
        # A bare date from an export has no zone. ensure_utc() applies the
        # UTC assumption in one place, so the assumption is visible rather
        # than repeated at four call sites.
        return ensure_utc(parsed)
    return None


def _normalize_keys(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        clean = str(key).strip().lower().replace(" ", "_").replace("-", "_")
        result[COLUMN_ALIASES.get(clean, clean)] = value
    return result


def _to_posting(row: dict[str, Any], line: int) -> RawPosting:
    row = _normalize_keys(row)
    missing = [c for c in REQUIRED_COLUMNS if not str(row.get(c) or "").strip()]
    if missing:
        raise ValidationError(f"row {line}: missing required field(s): {', '.join(missing)}")

    def get(name: str) -> str | None:
        value = row.get(name)
        text = str(value).strip() if value is not None else ""
        return text or None

    return RawPosting(
        title=str(row["title"]).strip(),
        url=str(row["url"]).strip(),
        company_name=get("company_name"),
        external_id=get("external_id"),
        location=get("location"),
        description=get("description"),
        salary_text=get("salary_text"),
        posted_at=_parse_datetime(get("posted_at")),
        expires_at=_parse_datetime(get("expires_at")),
    )


def parse_csv(content: str) -> list[RawPosting]:
    """Parse a CSV export into postings.

    Rejects the whole file on a malformed row rather than importing part of
    it: a partial import is harder to reason about than a failed one.
    """
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValidationError("CSV has no header row")

    headers = {COLUMN_ALIASES.get(
        (h or "").strip().lower().replace(" ", "_").replace("-", "_"),
        (h or "").strip().lower().replace(" ", "_").replace("-", "_"),
    ) for h in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise ValidationError(
            f"CSV is missing required column(s): {', '.join(missing)}. "
            f"Recognised columns: {', '.join(KNOWN_COLUMNS)}"
        )

    postings: list[RawPosting] = []
    for line, row in enumerate(reader, start=2):
        if not any(str(v or "").strip() for v in row.values()):
            continue  # blank line
        postings.append(_to_posting(row, line))
        if len(postings) > MAX_ROWS:
            raise ValidationError(f"import exceeds {MAX_ROWS} rows; split the file")
    if not postings:
        raise ValidationError("CSV contained no data rows")
    return postings


def parse_json(content: str) -> list[RawPosting]:
    """Parse a JSON array of postings, or an object with a `jobs` key."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc

    if isinstance(data, dict):
        for key in ("jobs", "results", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValidationError(
                "JSON object has no array under 'jobs', 'results', 'data' or 'items'"
            )
    if not isinstance(data, list):
        raise ValidationError("JSON must be an array of postings")
    if len(data) > MAX_ROWS:
        raise ValidationError(f"import exceeds {MAX_ROWS} rows; split the file")
    if not data:
        raise ValidationError("JSON contained no postings")

    return [_to_posting(row, index) for index, row in enumerate(data, start=1)
            if isinstance(row, dict)]
