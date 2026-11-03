"""M5 unknown evidence never becomes numeric credit or a mismatch."""

from datetime import date
from uuid import uuid4

import pytest

from packages.agents.resume_optimizer.assessment import Alignment, align, assess
from packages.agents.resume_optimizer.evidence import FactView
from packages.agents.resume_optimizer.requirements import Necessity, Requirement, RequirementKind

pytestmark = [pytest.mark.unit, pytest.mark.safety]
TODAY = date(2026, 1, 1)


def requirement(text="Experience with AWS", years=None):
    return Requirement(text, Necessity.REQUIRED, RequirementKind.SKILL, years_min=years)


def alignments(value=True):
    return {
        name: Alignment(value, "explicit fixture evidence")
        for name in ("seniority", "domain", "location")
    }


def evaluate(reqs, facts, dimensions=None):
    return assess(
        reqs,
        facts,
        today=TODAY,
        extraction_confident=True,
        alignments=alignments() if dimensions is None else dimensions,
    )


def test_empty_requirements_are_unresolved_not_100():
    result = evaluate([], [])
    assert result.status == "insufficient_evidence"
    assert result.breakdown is None
    assert "no requirements" in result.reasons[0]


@pytest.mark.parametrize("name", ["seniority", "domain", "location", "all"])
def test_material_unknown_alignment_prevents_scoring(name):
    dimensions = alignments()
    for key in dimensions:
        if name in (key, "all"):
            dimensions[key] = Alignment(None, "unstated")
    result = evaluate([requirement()], [FactView(uuid4(), "AWS")], dimensions)
    assert result.status == "insufficient_evidence"
    assert result.breakdown is None


def test_known_and_unknown_requirements_preserve_evidence_without_penalty():
    fact = FactView(uuid4(), "AWS")
    result = evaluate([requirement(), requirement("Experience with Terraform")], [fact])
    assert result.breakdown is None
    rows = result.evidence()["assessment"]["requirements"]
    assert [row["status"] for row in rows] == ["match", "unknown"]
    assert result.matrix.rows[0].fact_ids == [str(fact.id)]


def test_supported_match_scores_normally_without_preferred_or_salary_information():
    result = evaluate([requirement()], [FactView(uuid4(), "AWS")])
    assert result.status == "assessed"
    assert result.breakdown.score == 100


def test_explicit_alignment_mismatches_are_distinct_from_unknown():
    result = evaluate([requirement()], [FactView(uuid4(), "AWS")], alignments(False))
    assert result.status == "assessed"
    assert result.breakdown.score < 55
    assert all(
        row["status"] == "mismatch"
        for row in result.evidence()["assessment"]["alignments"].values()
    )


def test_dated_partial_support_is_assessable_but_missing_dates_are_unknown():
    fact = FactView(uuid4(), "AWS", {"start_date": "2025-01-01", "end_date": "2026-01-01"})
    result = evaluate([requirement(years=5)], [fact])
    assert result.status == "assessed"
    assert 0 < result.breakdown.required_ratio < 1
    undated = evaluate([requirement(years=5)], [FactView(uuid4(), "AWS")])
    assert undated.status == "insufficient_evidence"
    assert undated.breakdown is None


def test_no_preferences_are_inferred_from_default_constraints():
    result = align(
        [],
        seniority="senior",
        title="Cloud Engineer",
        target_roles=[],
        is_remote=True,
        is_hybrid=False,
        location_state=None,
    )
    assert all(row.value is None for row in result.values())


def test_conflicting_verified_preferences_are_unknown():
    facts = [FactView(uuid4(), "preference", {"accepts_remote": value}) for value in (True, False)]
    result = align(
        facts,
        seniority="unknown",
        title="",
        target_roles=[],
        is_remote=True,
        is_hybrid=False,
        location_state=None,
    )
    assert result["location"].value is None


def test_low_confidence_extraction_is_not_completed_by_inference():
    result = assess(
        [requirement()],
        [FactView(uuid4(), "AWS")],
        today=TODAY,
        extraction_confident=False,
        alignments=alignments(),
    )
    assert result.status == "insufficient_evidence"
    assert result.breakdown is None
