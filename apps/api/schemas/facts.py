"""Pydantic schemas for the profile_facts endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models import FactStatus, FactType

FactText = Annotated[str, Field(min_length=1, max_length=2000)]

FACT_SOURCE_TABLES = Literal[
    "experiences", "educations", "certifications", "skills", "projects", "profiles"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FactCreate(StrictModel):
    """Create a fact. Always lands as DRAFT.

    ``status`` is deliberately absent: a fact cannot be created verified.
    Verification is a separate endpoint so that it is always a distinct,
    attributable human action.
    """

    fact_type: FactType
    text: FactText
    source_table: FACT_SOURCE_TABLES
    source_id: UUID
    structured: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Every numeral that may legitimately appear in generated content. "
            "The numeral guard checks resume bullets against exactly this field."
        ),
    )
    confidence: str | None = Field(default=None, max_length=500)
    valid_from: date | None = None
    valid_to: date | None = None


class FactUpdate(StrictModel):
    """Edit a draft fact. Verified facts must be un-verified first."""

    text: FactText | None = None
    fact_type: FactType | None = None
    structured: dict[str, Any] | None = None
    confidence: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class FactSourceRepair(StrictModel):
    """Explicit replacement of a dangling source; the original fact is retained."""

    source_id: UUID
    expected_updated_at: datetime
    note: str = Field(min_length=1, max_length=1000)


class FactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    fact_type: FactType
    text: str
    structured: dict[str, Any]
    source_table: str
    source_id: UUID
    status: FactStatus
    confidence: str | None
    valid_from: date | None
    valid_to: date | None
    usage_count: int
    verified_by: UUID | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_citable(self) -> bool:
        return self.status == FactStatus.VERIFIED


class FactVerifyRequest(StrictModel):
    #: The verifier is always the authenticated owner. The field remains only
    #: so older clients keep working; any other value is refused.
    verified_by: UUID | None = None
    note: str | None = Field(default=None, max_length=1000)


class FactSuggestRequest(StrictModel):
    source_table: FACT_SOURCE_TABLES
    source_id: UUID


class FactSummary(BaseModel):
    total: int
    by_status: dict[str, int]
    citable: int
    citable_by_type: dict[str, int]
    never_used: int
    draft_awaiting_verification: int


class FactEvidenceCreate(StrictModel):
    kind: Literal["document", "url", "attestation", "credential"]
    url: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    artifact_id: UUID | None = None
    #: Always the authenticated owner; any other value is refused.
    added_by: UUID | None = None


class FactEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fact_id: UUID
    kind: str
    artifact_id: UUID | None
    url: str | None
    note: str | None
    added_by: UUID | None
    created_at: datetime
