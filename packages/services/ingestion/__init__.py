"""Deterministic job ingestion: policy gate, normalization, dedupe.

No LLM and no browser automation live in this package. See
docs/SECURITY.md §7 for why the connector set is deliberately small.
"""

from packages.services.ingestion.dedupe import (
    DuplicateMatch,
    DuplicateReason,
    canonical_url,
    find_duplicate,
    title_similarity,
)
from packages.services.ingestion.normalize import (
    NormalizedJob,
    content_hash,
    normalize_company_name,
    normalize_job,
    normalize_title,
    parse_location,
    parse_salary,
)
from packages.services.ingestion.pipeline import IngestionPipeline
from packages.services.ingestion.types import (
    IngestOutcome,
    IngestReport,
    RawPosting,
)
from packages.services.ingestion.policy import (
    BlockReason,
    PolicyDecision,
    PolicySnapshot,
    PolicyVerdict,
    blocked_policy,
    evaluate_ingestion,
    evaluate_submission,
    extract_domain,
)

__all__ = [
    "BlockReason",
    "DuplicateMatch",
    "DuplicateReason",
    "IngestOutcome",
    "IngestReport",
    "IngestionPipeline",
    "NormalizedJob",
    "PolicyDecision",
    "PolicySnapshot",
    "PolicyVerdict",
    "RawPosting",
    "blocked_policy",
    "canonical_url",
    "content_hash",
    "evaluate_ingestion",
    "evaluate_submission",
    "extract_domain",
    "find_duplicate",
    "normalize_company_name",
    "normalize_job",
    "normalize_title",
    "parse_location",
    "parse_salary",
    "title_similarity",
]
