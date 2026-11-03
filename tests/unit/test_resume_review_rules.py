"""C2 review rules that hold without a database.

Readiness and staleness are the two decisions that make an approval mean
something, and neither needs a connection to state. Testing them here makes
each rule cheap to pin individually, so the integration suite can spend its
time on the transactional behaviour rather than re-proving arithmetic.

The stand-ins below are the smallest objects the pure functions actually read.
They are not ORM mocks pretending to be rows: if a rule ever starts reading a
column these do not have, the test fails loudly rather than passing against a
fiction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from packages.core.models import ClaimVerdict
from packages.core.services.resume_review import (
    build_guardrail_flags,
    content_paths,
    evaluate_readiness,
    snapshot_matches,
)

#: `safety` as well as `unit`: these rules are what stops a resume with an
#: unsupported claim reaching a human as "ready to approve".
pytestmark = [pytest.mark.unit, pytest.mark.safety]

VERIFIED_AT = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
RESUME_ID = UUID("01930000-0000-7000-8000-000000000001")


@dataclass
class FakeClaim:
    """Only the four attributes the readiness rules read."""

    path: str
    verdict: ClaimVerdict = ClaimVerdict.SUPPORTED
    verified_at: datetime | None = VERIFIED_AT
    section: str = "experience"


@dataclass
class FakeResume:
    id: UUID = RESUME_ID
    version: int = 1


def content_for(*paths: str) -> dict[str, Any]:
    """Rebuild B6's `{section: {index: entry}}` shape from claim paths."""
    content: dict[str, Any] = {}
    for path in paths:
        _, section, index = path.split("/")
        content.setdefault(section, {})[index] = {"text": "x", "order": int(index)}
    return content


def ready_case() -> tuple[list[FakeClaim], dict[str, Any]]:
    paths = ["/summary/0", "/summary/1", "/experience/0"]
    return [FakeClaim(path=p) for p in paths], content_for(*paths)


# --- the happy path ---------------------------------------------------------


def test_a_clean_verified_resume_is_ready() -> None:
    claims, content = ready_case()
    result = evaluate_readiness(verification_status="passed", claims=claims, content=content)
    assert result.ready
    assert result.reason is None


# --- every refusal ----------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_only_a_passed_verification_is_reviewable(status: str) -> None:
    claims, content = ready_case()
    result = evaluate_readiness(verification_status=status, claims=claims, content=content)
    assert not result.ready
    assert status in (result.reason or "")


def test_a_resume_with_no_claims_is_refused() -> None:
    """Nothing to review is not the same as nothing wrong."""
    result = evaluate_readiness(verification_status="passed", claims=[], content={})
    assert not result.ready
    assert "no recorded claims" in (result.reason or "")


@pytest.mark.parametrize(
    "verdict",
    [ClaimVerdict.UNSUPPORTED, ClaimVerdict.OVERREACH, ClaimVerdict.NOT_CHECKED],
)
def test_any_non_supported_verdict_blocks_review(verdict: ClaimVerdict) -> None:
    """`passed` on the resume row is not enough if a claim disagrees.

    The two can drift - a claim edited after the fact, a partial re-verify -
    and when they do, the per-claim verdicts win. A human is approving the
    sentences, not the summary field.
    """
    claims, content = ready_case()
    claims[1].verdict = verdict
    result = evaluate_readiness(verification_status="passed", claims=claims, content=content)
    assert not result.ready
    assert str(verdict) in (result.reason or "")


def test_a_claim_without_a_verification_timestamp_blocks_review() -> None:
    claims, content = ready_case()
    claims[0].verified_at = None
    result = evaluate_readiness(verification_status="passed", claims=claims, content=content)
    assert not result.ready
    assert "verification timestamp" in (result.reason or "")


def test_a_rendered_line_with_no_claim_blocks_review() -> None:
    """Coverage, forwards: text nobody verified must not slip through."""
    claims, content = ready_case()
    content["experience"]["1"] = {"text": "Unverified addition.", "order": 1}
    result = evaluate_readiness(verification_status="passed", claims=claims, content=content)
    assert not result.ready
    assert "/experience/1" in (result.reason or "")


def test_a_claim_with_no_rendered_line_blocks_review() -> None:
    """Coverage, backwards: a verdict about text that is gone is stale."""
    claims, content = ready_case()
    claims.append(FakeClaim(path="/experience/9"))
    result = evaluate_readiness(verification_status="passed", claims=claims, content=content)
    assert not result.ready
    assert "/experience/9" in (result.reason or "")


def test_content_paths_ignores_malformed_sections() -> None:
    """A section stored as something other than an object is not a path."""
    assert content_paths({"summary": {"0": {}}, "junk": "not-a-section"}) == {"/summary/0"}


# --- staleness --------------------------------------------------------------


def test_snapshot_matches_only_on_id_and_version_together() -> None:
    resume = FakeResume(version=2)
    assert snapshot_matches(
        {"resume_id": str(RESUME_ID), "resume_version": 2},
        resume,  # type: ignore[arg-type]
    )
    # Right resume, wrong version: this is exactly the regeneration case the
    # binding exists to catch.
    assert not snapshot_matches(
        {"resume_id": str(RESUME_ID), "resume_version": 1},
        resume,  # type: ignore[arg-type]
    )
    # Right version number, different resume.
    assert not snapshot_matches(
        {
            "resume_id": "01930000-0000-7000-8000-0000000000ff",
            "resume_version": 2,
        },
        resume,  # type: ignore[arg-type]
    )


def test_an_empty_snapshot_never_matches() -> None:
    assert not snapshot_matches({}, FakeResume())  # type: ignore[arg-type]


# --- guardrail flags --------------------------------------------------------


def test_guardrail_flags_tally_every_verdict() -> None:
    claims, _ = ready_case()
    flags = build_guardrail_flags(claims)  # type: ignore[arg-type]
    assert flags["total_claims"] == 3
    assert flags["verdict_counts"]["supported"] == 3
    # Every verdict is present as a key, including the zeroes: a reader should
    # not have to know which verdicts exist to interpret the record.
    assert set(flags["verdict_counts"]) == {str(v) for v in ClaimVerdict}
