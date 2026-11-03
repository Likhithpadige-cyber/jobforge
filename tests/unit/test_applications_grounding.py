"""M9 - classification and grounding, pure and offline (no database, no model).

Mirrors the M8 renderer suites' shape: every function here is deterministic,
so every test is an exact input -> exact output assertion, not a mock.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.core.models.enums import AnswerSource, QuestionClass
from packages.core.models.facts import ProfileFact
from packages.core.models.identity import Experience, Profile
from packages.core.services.applications import (
    classify_question,
    draft_answer,
    draft_employment_type_answer,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "question",
    [
        "Are you legally authorized to work in the United States?",
        "What is your date of birth?",
    ],
)
def test_sensitive_beats_every_other_pattern(question: str) -> None:
    """The one rule nothing below classify_question may override."""
    assert classify_question(question, "text") is QuestionClass.SENSITIVE


@pytest.mark.parametrize(
    "question",
    ["What is your full name?", "What is your email address?", "What city do you live in?"],
)
def test_profile_field_questions_are_factual_safe(question: str) -> None:
    assert classify_question(question, "text") is QuestionClass.FACTUAL_SAFE


def test_years_of_experience_is_derived() -> None:
    assert (
        classify_question("How many years of experience do you have with AWS?", "number")
        is QuestionClass.DERIVED
    )


@pytest.mark.parametrize(
    "question",
    [
        "What are your salary expectations?",
        "What is your desired start date?",
        "Why do you want to work here?",
    ],
)
def test_forward_looking_preferences_are_judgement(question: str) -> None:
    assert classify_question(question, "text") is QuestionClass.JUDGEMENT


def test_open_essay_field_defaults_to_judgement() -> None:
    """No recognised phrasing at all, but the input type says essay."""
    assert classify_question("Tell us something.", "textarea") is QuestionClass.JUDGEMENT


def test_unrecognised_question_is_uncertain() -> None:
    assert classify_question("What is your favorite color?", "text") is QuestionClass.UNCERTAIN


def test_sensitive_cannot_be_shadowed_by_a_factual_pattern() -> None:
    """A question matching both a profile-field pattern and a sensitive one
    must land on sensitive - escalation always wins, in either order."""
    assert (
        classify_question("What is your email? (work authorization required)", "text")
        is QuestionClass.SENSITIVE
    )


# ------------------------------------------------------------------- grounding


def _profile(**overrides: object) -> Profile:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "user_id": uuid4(),
        "full_name": "Jordan Rivera",
        "email": "jordan@example.invalid",
        "phone": None,
        "location_city": "Austin",
        "location_state": "Texas",
        "location_country": "USA",
        "willing_to_relocate": True,
        "target_roles": [],
        "accepted_employment_types": None,
        "links": {},
    }
    defaults.update(overrides)
    return Profile(**defaults)  # type: ignore[arg-type]


def test_sensitive_is_always_blank_regardless_of_profile_data() -> None:
    drafted = draft_answer(
        "Are you legally authorized to work in the United States?",
        QuestionClass.SENSITIVE,
        profile=_profile(),
        experiences=[],
        facts=[],
    )
    assert drafted.source is AnswerSource.BLANK
    assert drafted.value is None
    assert drafted.requires_human is True
    assert drafted.fact_ids == []


def test_judgement_is_always_blank() -> None:
    drafted = draft_answer(
        "What are your salary expectations?",
        QuestionClass.JUDGEMENT,
        profile=_profile(),
        experiences=[],
        facts=[],
    )
    assert drafted.source is AnswerSource.BLANK
    assert drafted.value is None
    assert drafted.requires_human is True


def test_factual_safe_grounds_from_a_profile_column() -> None:
    drafted = draft_answer(
        "What city do you live in?",
        QuestionClass.FACTUAL_SAFE,
        profile=_profile(location_city="Denver"),
        experiences=[],
        facts=[],
    )
    assert drafted.source is AnswerSource.DERIVED
    assert drafted.value == "Denver"
    assert drafted.fact_ids == []
    assert drafted.requires_human is False
    assert drafted.value_structured["derived_from"] == "profile.location_city"


def test_factual_safe_never_cites_a_fake_fact() -> None:
    """The whole provenance rule in one assertion: a profile-column answer
    must never carry a fact_id."""
    drafted = draft_answer(
        "What is your email address?",
        QuestionClass.FACTUAL_SAFE,
        profile=_profile(email="jordan@example.invalid"),
        experiences=[],
        facts=[],
    )
    assert drafted.fact_ids == []
    assert drafted.source is not AnswerSource.AGENT


def test_factual_safe_blanks_when_the_profile_field_is_unset() -> None:
    """A recognised pattern with nothing behind it is a blank, never a guess."""
    drafted = draft_answer(
        "What is your phone number?",
        QuestionClass.FACTUAL_SAFE,
        profile=_profile(phone=None),
        experiences=[],
        facts=[],
    )
    assert drafted.source is AnswerSource.BLANK
    assert drafted.value is None
    assert drafted.requires_human is True


def _experience(start: date, end: date | None) -> Experience:
    return Experience(
        id=uuid4(),
        profile_id=uuid4(),
        company_name="Example Corp",
        title="Engineer",
        start_date=start,
        end_date=end,
        deleted_at=None,
    )  # type: ignore[arg-type]


def test_derived_computes_years_of_experience_from_date_span() -> None:
    experiences = [_experience(date(2020, 1, 1), date(2023, 1, 1))]
    drafted = draft_answer(
        "How many years of experience do you have?",
        QuestionClass.DERIVED,
        profile=_profile(),
        experiences=experiences,
        facts=[],
    )
    assert drafted.source is AnswerSource.DERIVED
    assert drafted.fact_ids == []
    assert drafted.requires_human is False
    assert drafted.value is not None
    assert Decimal(drafted.value) == Decimal("3.0")
    assert drafted.value_structured["derived_from"] == "experiences"


def test_derived_takes_the_outer_span_not_a_sum_of_overlapping_roles() -> None:
    """Two concurrent roles must not double-count - the outer bound only."""
    experiences = [
        _experience(date(2020, 1, 1), date(2022, 1, 1)),
        _experience(date(2021, 1, 1), date(2023, 1, 1)),
    ]
    drafted = draft_answer(
        "years of experience",
        QuestionClass.DERIVED,
        profile=_profile(),
        experiences=experiences,
        facts=[],
    )
    assert drafted.value is not None
    assert Decimal(drafted.value) == Decimal("3.0")


def test_derived_blanks_with_no_experience_rows() -> None:
    drafted = draft_answer(
        "years of experience",
        QuestionClass.DERIVED,
        profile=_profile(),
        experiences=[],
        facts=[],
    )
    assert drafted.source is AnswerSource.BLANK
    assert drafted.requires_human is True


def _fact(text: str) -> ProfileFact:
    return ProfileFact(
        id=uuid4(),
        profile_id=uuid4(),
        fact_type="certification",
        text=text,
        source_table="certifications",
        source_id=uuid4(),
        status="verified",
    )  # type: ignore[arg-type]


def test_uncertain_promotes_to_agent_on_a_confident_fact_match() -> None:
    fact = _fact("AWS Certified Solutions Architect")
    drafted = draft_answer(
        "Do you hold an AWS Certified Solutions Architect credential?",
        QuestionClass.UNCERTAIN,
        profile=_profile(),
        experiences=[],
        facts=[fact],
    )
    assert drafted.source is AnswerSource.AGENT
    assert drafted.fact_ids == [fact.id]
    assert drafted.requires_human is False
    assert drafted.confidence is not None


def test_unanswerable_stays_blank_with_no_matching_fact() -> None:
    drafted = draft_answer(
        "Do you hold a CISSP certification?",
        QuestionClass.UNANSWERABLE,
        profile=_profile(),
        experiences=[],
        facts=[_fact("PMP Certified")],
    )
    assert drafted.source is AnswerSource.BLANK
    assert drafted.fact_ids == []
    assert drafted.requires_human is True


def test_agent_answer_only_ever_cites_facts_it_was_given() -> None:
    """Grounding never reaches outside the `facts` list it was handed - the
    caller (prepare_application) is the one place profile scoping happens."""
    own_fact = _fact("AWS Certified Solutions Architect")
    drafted = draft_answer(
        "Do you hold an AWS Certified Solutions Architect credential?",
        QuestionClass.UNCERTAIN,
        profile=_profile(),
        experiences=[],
        facts=[own_fact],
    )
    assert drafted.fact_ids == [own_fact.id]


# ------------------------------------------------- employment-type preference


def test_explicit_accepted_type_may_advance() -> None:
    drafted = draft_employment_type_answer(
        job_employment_type="full_time", accepted_employment_types=["full_time"]
    )
    assert drafted.source is AnswerSource.DERIVED
    assert drafted.value == "true"
    assert drafted.requires_human is False
    assert drafted.fact_ids == []


@pytest.mark.parametrize("job_type", ["contract", "part_time", "internship", "temporary"])
def test_explicit_unaccepted_type_does_not_auto_advance(job_type: str) -> None:
    drafted = draft_employment_type_answer(
        job_employment_type=job_type, accepted_employment_types=["full_time"]
    )
    assert drafted.value == "false"
    assert drafted.requires_human is True
    assert drafted.source is AnswerSource.DERIVED


def test_missing_type_is_unresolved_not_inferred_full_time() -> None:
    """The rule stated by name: never infer full-time merely because a
    posting looks like an ordinary salaried job."""
    drafted = draft_employment_type_answer(
        job_employment_type=None, accepted_employment_types=["full_time"]
    )
    assert drafted.value is None
    assert drafted.requires_human is True
    assert drafted.source is AnswerSource.DERIVED


def test_employment_type_answer_never_cites_a_fact() -> None:
    """A candidate preference is never mistaken for verified-fact evidence."""
    for job_type in ("full_time", "contract", None):
        drafted = draft_employment_type_answer(
            job_employment_type=job_type, accepted_employment_types=["full_time"]
        )
        assert drafted.fact_ids == []
        assert drafted.source is not AnswerSource.AGENT
