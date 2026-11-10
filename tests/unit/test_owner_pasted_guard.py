"""The owner-provided policy exemption cannot be applied to any other source."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from packages.core.models import IngestionMethod, Source
from packages.services.ingestion.pipeline import (
    OWNER_PASTED_DOMAIN,
    OWNER_PASTED_SLUG,
    IngestionPipeline,
)
from packages.services.ingestion.types import RawPosting
from packages.shared.errors import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.safety]

RAW = RawPosting(title="Synthetic role", url="https://jobs.example.invalid/1")


@pytest.mark.parametrize(
    ("slug", "domain"),
    [
        ("connector", "jobs.example.invalid"),
        (OWNER_PASTED_SLUG, "jobs.example.invalid"),
        ("connector", OWNER_PASTED_DOMAIN),
    ],
)
async def test_owner_provided_is_refused_for_any_other_source(slug: str, domain: str) -> None:
    pipeline = IngestionPipeline(MagicMock())
    source = Source(
        slug=slug, domain=domain, ingestion_method=IngestionMethod.MANUAL_IMPORT, enabled=True
    )
    with pytest.raises(ValidationError, match="owner-pasted source"):
        await pipeline.ingest_one(source, RAW, owner_provided=True)
