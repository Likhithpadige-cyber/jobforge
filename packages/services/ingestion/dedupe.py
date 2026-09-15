"""Duplicate detection.

Job boards syndicate aggressively: the same posting shows up under several
sources, with a different requisition number, a slightly different title, and
sometimes a rewritten description. Every duplicate that reaches the evaluator
costs an LLM call and, worse, risks a second application to the same role.

Four strategies, cheapest first. All deterministic and all pure - the caller
supplies candidates from the database, this module only decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


class DuplicateReason(StrEnum):
    EXACT_CONTENT = "exact_content"
    SAME_SOURCE_ID = "same_source_id"
    SAME_URL = "same_url"
    SAME_ROLE_AT_COMPANY = "same_role_at_company"


class JobLike(Protocol):
    """Minimal shape needed for comparison - a row or a NormalizedJob."""

    id: UUID
    normalized_title: str | None
    description_hash: str | None
    url: str
    external_id: str | None
    company_id: UUID | None
    posted_at: datetime | None


@dataclass(frozen=True)
class DuplicateMatch:
    canonical_id: UUID
    reason: DuplicateReason
    similarity: float
    note: str


#: Tracking parameters that change per referral but not per posting.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "src", "source", "ref", "referrer", "gh_src", "trk", "trackingid",
    "lipi", "refid", "jobsearchtracking", "sessionid", "cid", "fbclid", "gclid",
})


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same posting compares equal.

    The fragment and trailing slash go too; neither identifies a posting.
    """
    parts = urlsplit(url.strip())
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and pair.split("=")[0].lower() not in TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _tokens(value: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+#]+", value.lower()) if len(t) > 1}


def title_similarity(left: str, right: str) -> float:
    """Jaccard similarity over title tokens.

    Chosen over edit distance because titles differ by inserted or removed
    words ("Senior Cloud Engineer" vs "Cloud Engineer, Senior - AWS") far more
    often than by character-level typos.
    """
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


#: Above this, two postings at the same company are treated as the same role.
#: Deliberately high: a false positive here means never applying to a real
#: second opening, which is worse than one wasted evaluation.
TITLE_SIMILARITY_THRESHOLD = 0.82


def find_duplicate(
    candidate: JobLike,
    existing: list[JobLike],
    *,
    same_source_id: bool = True,
    title_threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> DuplicateMatch | None:
    """Return the canonical job this candidate duplicates, or None.

    `existing` should already be narrowed by the caller (same company, or
    same source, recent window) - this function does not query.
    """
    canonical_candidate_url = canonical_url(candidate.url)

    # 1. Identical normalized content. The strongest signal.
    if candidate.description_hash:
        for other in existing:
            if other.id == getattr(candidate, "id", None):
                continue
            if other.description_hash == candidate.description_hash:
                return DuplicateMatch(
                    other.id, DuplicateReason.EXACT_CONTENT, 1.0,
                    "identical normalized title, company and description",
                )

    # 2. Same source-assigned id. Unambiguous within one source.
    if same_source_id and candidate.external_id:
        for other in existing:
            if other.id == getattr(candidate, "id", None):
                continue
            if other.external_id and other.external_id == candidate.external_id:
                return DuplicateMatch(
                    other.id, DuplicateReason.SAME_SOURCE_ID, 1.0,
                    f"same external_id {candidate.external_id!r}",
                )

    # 3. Same canonical URL after stripping tracking parameters.
    for other in existing:
        if other.id == getattr(candidate, "id", None):
            continue
        if canonical_url(other.url) == canonical_candidate_url:
            return DuplicateMatch(
                other.id, DuplicateReason.SAME_URL, 1.0,
                "same URL once tracking parameters are removed",
            )

    # 4. Same company, near-identical title. The fuzzy case.
    if candidate.company_id and candidate.normalized_title:
        best: tuple[float, JobLike] | None = None
        for other in existing:
            if other.id == getattr(candidate, "id", None):
                continue
            if other.company_id != candidate.company_id or not other.normalized_title:
                continue
            score = title_similarity(candidate.normalized_title, other.normalized_title)
            if score >= title_threshold and (best is None or score > best[0]):
                best = (score, other)
        if best:
            score, other = best
            return DuplicateMatch(
                other.id, DuplicateReason.SAME_ROLE_AT_COMPANY, round(score, 3),
                f"same company, title similarity {score:.2f}",
            )

    return None


def choose_canonical(left: JobLike, right: JobLike) -> tuple[JobLike, JobLike]:
    """Decide which of two duplicates is the canonical row.

    Prefers the posting with a real `posted_at`, then the older one, then the
    one with an external id. Returns (canonical, duplicate).
    """
    if (left.posted_at is None) != (right.posted_at is None):
        return (left, right) if left.posted_at is not None else (right, left)
    if left.posted_at and right.posted_at and left.posted_at != right.posted_at:
        return (left, right) if left.posted_at < right.posted_at else (right, left)
    if bool(left.external_id) != bool(right.external_id):
        return (left, right) if left.external_id else (right, left)
    return (left, right)
