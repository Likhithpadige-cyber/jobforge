"""The policy gate must fail closed in every direction."""

from __future__ import annotations

from datetime import date, datetime, UTC

import pytest

from packages.core.models import IngestionMethod, SubmissionMethod
from packages.services.ingestion.policy import (
    BlockReason,
    PolicyDecision,
    PolicySnapshot,
    blocked_policy,
    evaluate_ingestion,
    evaluate_submission,
    extract_domain,
)

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.example.invalid/x", "jobs.example.invalid"),
        ("https://www.example.invalid/x", "example.invalid"),
        ("HTTPS://Example.Invalid:8443/x", "example.invalid"),
        ("https://user:pw@example.invalid/x", "example.invalid"),
    ],
)
def test_extract_domain(url: str, expected: str) -> None:
    assert extract_domain(url) == expected


def test_unknown_domain_is_blocked_not_an_exception() -> None:
    """A raising lookup invites a try/except that quietly reopens the gate."""
    verdict = evaluate_ingestion(
        blocked_policy("never-seen.example.invalid"), IngestionMethod.PUBLIC_FEED
    )
    assert verdict.decision is PolicyDecision.BLOCKED
    assert verdict.reason is BlockReason.NO_POLICY
    assert verdict.permitted is False


def test_every_default_is_restrictive() -> None:
    policy = blocked_policy("example.invalid")
    assert policy.automation_allowed is False
    assert policy.ingestion_method is IngestionMethod.BLOCKED
    assert policy.submission_method is SubmissionMethod.HUMAN_HANDOFF
    assert policy.requires_login is True
    assert policy.exists is False


def test_disabled_source_is_blocked_even_with_a_good_policy() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=True,
        ingestion_method=IngestionMethod.PUBLIC_FEED, requires_login=False,
    )
    verdict = evaluate_ingestion(policy, IngestionMethod.PUBLIC_FEED, source_enabled=False)
    assert verdict.reason is BlockReason.SOURCE_DISABLED


def test_method_mismatch_is_blocked() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=True,
        ingestion_method=IngestionMethod.MANUAL_IMPORT,
    )
    verdict = evaluate_ingestion(policy, IngestionMethod.PUBLIC_FEED)
    assert verdict.reason is BlockReason.METHOD_BLOCKED


def test_automated_method_needs_automation_permission() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=False,
        ingestion_method=IngestionMethod.PUBLIC_FEED, requires_login=False,
    )
    assert evaluate_ingestion(
        policy, IngestionMethod.PUBLIC_FEED
    ).reason is BlockReason.AUTOMATION_FORBIDDEN


def test_login_required_blocks_automated_ingestion() -> None:
    """Automated authentication is never performed, so a login wall ends it."""
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=True,
        ingestion_method=IngestionMethod.PUBLIC_FEED, requires_login=True,
    )
    assert evaluate_ingestion(
        policy, IngestionMethod.PUBLIC_FEED
    ).reason is BlockReason.LOGIN_REQUIRED


def test_expired_review_blocks_ingestion() -> None:
    """Terms change; a stale review is not a review."""
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=True,
        ingestion_method=IngestionMethod.PUBLIC_FEED, requires_login=False,
        reviewed_at=datetime(2025, 1, 1, tzinfo=UTC),
        next_review_due=date(2025, 6, 1),
    )
    verdict = evaluate_ingestion(policy, IngestionMethod.PUBLIC_FEED,
                                 today=date(2026, 9, 9))
    assert verdict.reason is BlockReason.REVIEW_EXPIRED


def test_manual_import_does_not_need_automation_permission() -> None:
    """A human pasting a posting they are already reading is not automation."""
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=False,
        ingestion_method=IngestionMethod.MANUAL_IMPORT, requires_login=True,
    )
    verdict = evaluate_ingestion(policy, IngestionMethod.MANUAL_IMPORT)
    assert verdict.decision is PolicyDecision.ALLOWED


def test_extension_capture_does_not_need_automation_permission() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True,
        ingestion_method=IngestionMethod.EXTENSION_CAPTURE, requires_login=True,
    )
    assert evaluate_ingestion(
        policy, IngestionMethod.EXTENSION_CAPTURE
    ).decision is PolicyDecision.ALLOWED


def test_fully_permitted_feed_is_allowed() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=True,
        ingestion_method=IngestionMethod.PUBLIC_FEED, requires_login=False,
        reviewed_at=datetime(2026, 8, 1, tzinfo=UTC),
        next_review_due=date(2027, 8, 1),
    )
    verdict = evaluate_ingestion(policy, IngestionMethod.PUBLIC_FEED,
                                 today=date(2026, 9, 9))
    assert verdict.permitted is True


# ------------------------------------------------------------- submission
def test_submission_blocked_without_a_policy() -> None:
    assert evaluate_submission(blocked_policy("x.invalid")).decision is PolicyDecision.BLOCKED


def test_human_handoff_is_never_reported_as_allowed() -> None:
    """`.permitted` must be False for handoff, or a caller checking it could
    auto-submit to a site that requires a human."""
    policy = PolicySnapshot(
        domain="example.invalid", exists=True,
        submission_method=SubmissionMethod.HUMAN_HANDOFF,
    )
    verdict = evaluate_submission(policy)
    assert verdict.decision is PolicyDecision.REQUIRES_HUMAN
    assert verdict.permitted is False


def test_api_submission_requires_automation_permission() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True, automation_allowed=False,
        submission_method=SubmissionMethod.OFFICIAL_API,
    )
    assert evaluate_submission(policy).reason is BlockReason.AUTOMATION_FORBIDDEN


def test_email_submission_is_allowed() -> None:
    policy = PolicySnapshot(
        domain="example.invalid", exists=True,
        submission_method=SubmissionMethod.EMAIL,
    )
    assert evaluate_submission(policy).permitted is True
