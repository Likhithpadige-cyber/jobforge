"""The hard-coded sensitive-question classifier (AGENT_DESIGN.md §4.2).

Escalate-only: every case here proves a recognized pattern still fires
(catches a false negative before it ships) or that an ordinary question is
left alone. There is no de-escalation path to test, because none exists.
"""

from __future__ import annotations

import pytest

from packages.core.policies.sensitive_questions import is_sensitive

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "question",
    [
        "Are you legally authorized to work in the United States?",
        "Will you now or in the future require visa sponsorship?",
        "Do you hold an active security clearance?",
        "Have you ever been convicted of a felony?",
        "Do you have a disability that requires reasonable accommodation?",
        "Are you a protected veteran?",
        "Please indicate your race/ethnicity (optional).",
        "What is your gender?",
        "What is your date of birth?",
        "What was your most recent salary?",
        "Please provide your salary history for the past 3 positions.",
        "Are you a U.S. citizen?",
    ],
)
def test_recognized_sensitive_patterns_are_flagged(question: str) -> None:
    assert is_sensitive(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "What is your full name?",
        "What is your email address?",
        "What city do you currently live in?",
        "Do you have a Bachelor's degree in Computer Science?",
        "How many years of experience do you have with AWS?",
        "What is your expected start date?",
        "Why do you want to work here?",
        "What are your salary expectations for this role?",
    ],
)
def test_ordinary_questions_are_not_flagged(question: str) -> None:
    """Salary *expectation* (forward-looking) is `judgement`, not
    `sensitive` - only salary *history* is on the hard-coded list, per
    AGENT_DESIGN.md §4.2. Both still require a human; this classifier only
    decides which of the two human-required classes applies."""
    assert is_sensitive(question) is False


def test_matching_is_case_insensitive() -> None:
    assert is_sensitive("ARE YOU LEGALLY AUTHORIZED TO WORK IN THE US?") is True


def test_empty_string_is_not_sensitive() -> None:
    assert is_sensitive("") is False
