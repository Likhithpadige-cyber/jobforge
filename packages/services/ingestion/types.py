"""Pure data types for ingestion.

Kept separate from pipeline.py, which imports SQLAlchemy. A connector should
be usable - and testable - without a database anywhere in the picture, and a
CSV parser has no business depending on the ORM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

RESULT_FIELDS = {
    "created": "created",
    "duplicate": "duplicates",
    "updated": "updated",
    "rejected": "rejected",
}


@dataclass
class RawPosting:
    """One posting as supplied by a connector, before any processing."""

    title: str
    url: str
    company_name: str | None = None
    external_id: str | None = None
    location: str | None = None
    description: str | None = None
    salary_text: str | None = None
    posted_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class IngestOutcome:
    """What happened to one posting."""

    url: str
    result: str  # created | duplicate | updated | rejected
    job_id: UUID | None = None
    canonical_id: UUID | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    source_slug: str
    created: int = 0
    duplicates: int = 0
    updated: int = 0
    rejected: int = 0
    outcomes: list[IngestOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.duplicates + self.updated + self.rejected

    def record(self, outcome: IngestOutcome) -> None:
        if outcome.result not in RESULT_FIELDS:
            raise ValueError(f"unknown ingest result {outcome.result!r}")
        self.outcomes.append(outcome)
        attribute = RESULT_FIELDS[outcome.result]
        setattr(self, attribute, getattr(self, attribute) + 1)
