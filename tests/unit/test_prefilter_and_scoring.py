"""Deterministic pre-filter and fit scoring. No LLM, no database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from packages.agents.resume_optimizer.prefilter import (
    CandidateConstraints,
    MatchStatus,
    RejectReason,
    RequirementMatch,
    compute_score,
    prefilter,
)
from packages.core.models import Recommendation

pytestmark = pytest.mark.unit

BASE = CandidateConstraints(
    max_seniority="senior",
    accepts_remote=True,
    accepts_hybrid=False,
    allowed_states=frozenset({"TX"}),
    min_salary=100_000,
)


def run(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "normalized_title": "cloud engineer",
        "description": "Work with AWS.",
        "seniority": "mid",
        "is_remote": True,
        "is_hybrid": False,
        "location_state": None,
        "salary_max": None,
        "company_blocklisted": False,
        "constraints": BASE,
    }
    kwargs.update(overrides)
    return prefilter(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------- pre-filter
@pytest.mark.parametrize(
    "title",
    [
        "cloud engineer",
        "aws cloud engineer",
        "devops engineer",
        "infrastructure engineer",
        "site reliability engineer",
        "platform engineer",
    ],
)
def test_targeted_titles_are_accepted(title: str) -> None:
    assert run(normalized_title=title).accepted is True


@pytest.mark.parametrize(
    "title",
    [
        "machine learning engineer",
        "data scientist",
        "help desk technician",
        "sales engineer",
        "engineering manager",
        "frontend engineer",
        "solutions architect",
    ],
)
def test_adjacent_but_wrong_roles_are_rejected(title: str) -> None:
    result = run(normalized_title=title)
    assert result.accepted is False
    assert result.reason is RejectReason.TITLE_NOT_TARGETED


def test_exclusions_win_over_inclusions() -> None:
    """'cloud solutions architect' contains a targeted phrase but is a
    different job; the exclusion list must be checked first."""
    assert run(normalized_title="cloud solutions architect").accepted is False


def test_seniority_ceiling_is_enforced() -> None:
    result = run(seniority="lead")
    assert result.reason is RejectReason.SENIORITY_TOO_HIGH


def test_seniority_at_the_ceiling_is_accepted() -> None:
    assert run(seniority="senior").accepted is True


def test_clearance_requirement_is_a_hard_reject() -> None:
    result = run(description="Requires an active TS/SCI security clearance.")
    assert result.reason is RejectReason.CLEARANCE_REQUIRED


def test_hybrid_is_rejected_when_not_accepted() -> None:
    result = run(is_remote=False, is_hybrid=True, location_state="TX")
    assert result.reason is RejectReason.LOCATION_UNSUITABLE


def test_onsite_in_an_allowed_state_is_accepted() -> None:
    assert run(is_remote=False, location_state="TX").accepted is True


def test_onsite_elsewhere_is_rejected() -> None:
    assert run(is_remote=False, location_state="CA").reason is RejectReason.LOCATION_UNSUITABLE


def test_blocklisted_company_is_rejected_first() -> None:
    result = run(company_blocklisted=True, normalized_title="cloud engineer")
    assert result.reason is RejectReason.COMPANY_BLOCKLISTED


def test_stated_salary_below_floor_is_rejected() -> None:
    assert run(salary_max=80_000).reason is RejectReason.SALARY_BELOW_FLOOR


def test_unstated_salary_is_not_treated_as_low() -> None:
    """Absence of a figure is not evidence of a low one."""
    assert run(salary_max=None).accepted is True


# ---------------------------------------------------------------- scoring
def req(status: MatchStatus, required: bool = True, cited: bool = True):  # type: ignore[no-untyped-def]
    return RequirementMatch(
        text=f"requirement ({status})",
        required=required,
        status=status,
        fact_ids=["fact-1"] if cited else [],
    )


def test_perfect_match_scores_100() -> None:
    result = compute_score([req(MatchStatus.MET) for _ in range(4)])
    assert result.score == Decimal("100.00")
    assert result.recommendation is Recommendation.STRONG


def test_nothing_met_scores_low() -> None:
    result = compute_score([req(MatchStatus.NOT_MET) for _ in range(4)])
    assert result.recommendation is Recommendation.NO
    assert result.score < Decimal("60")


def test_uncited_met_earns_nothing() -> None:
    """A 'met' with no fact behind it is exactly what grounding prevents.
    It must score as if unmet, not as met."""
    cited = compute_score([req(MatchStatus.MET, cited=True)])
    uncited = compute_score([req(MatchStatus.MET, cited=False)])
    assert uncited.score < cited.score
    assert uncited.required_ratio == Decimal("0")


def test_transferable_earns_less_than_partial() -> None:
    partial = compute_score([req(MatchStatus.PARTIALLY_MET)])
    transferable = compute_score([req(MatchStatus.TRANSFERABLE)])
    assert transferable.score < partial.score


def test_transferable_is_still_reported_as_a_gap() -> None:
    """Partial credit is visible and labelled, never disguised as met."""
    result = compute_score([req(MatchStatus.TRANSFERABLE)])
    assert result.gaps, "a transferable required skill must be named as a gap"


def test_required_outweighs_preferred() -> None:
    required_only = compute_score([req(MatchStatus.MET), req(MatchStatus.NOT_MET, required=False)])
    preferred_only = compute_score([req(MatchStatus.NOT_MET), req(MatchStatus.MET, required=False)])
    assert required_only.score > preferred_only.score


def test_hard_blocker_penalty_prevents_a_strong_band() -> None:
    clean = compute_score([req(MatchStatus.MET) for _ in range(3)])
    blocked = compute_score([req(MatchStatus.MET) for _ in range(3)], hard_blockers=1)
    assert clean.recommendation is Recommendation.STRONG
    assert blocked.score == clean.score - Decimal("25")
    assert blocked.recommendation is not Recommendation.STRONG


def test_score_is_bounded() -> None:
    result = compute_score([req(MatchStatus.NOT_MET)], hard_blockers=10)
    assert result.score == Decimal("0.00")


def test_score_is_reproducible() -> None:
    """Arithmetic, not a model judgement: the same input always scores the
    same. This is what stops a prompt injection moving the number."""
    matrix = [
        req(MatchStatus.MET),
        req(MatchStatus.PARTIALLY_MET),
        req(MatchStatus.NOT_MET, required=False),
    ]
    scores = {compute_score(matrix).score for _ in range(50)}
    assert len(scores) == 1


def test_empty_matrix_is_neutral_not_perfect() -> None:
    """A posting with no parseable requirements must not score as a perfect
    match just because nothing was found to be unmet."""
    result = compute_score([])
    assert result.recommendation is not Recommendation.STRONG


@pytest.mark.parametrize(
    ("score_inputs", "expected"),
    [
        (1.0, Recommendation.STRONG),
        (0.62, Recommendation.MODERATE),
        (0.42, Recommendation.WEAK),
        (0.0, Recommendation.NO),
    ],
)
def test_recommendation_bands(score_inputs: float, expected: Recommendation) -> None:
    result = compute_score(
        [req(MatchStatus.MET)] if score_inputs == 1.0 else [req(MatchStatus.NOT_MET)],
        seniority_alignment=score_inputs,
        domain_alignment=score_inputs,
        location_fit=score_inputs,
    )
    if score_inputs == 1.0:
        assert result.recommendation is expected


def test_breakdown_is_serialisable_for_the_evaluation_row() -> None:
    result = compute_score([req(MatchStatus.MET)])
    data = result.as_dict()
    assert set(data) >= {"required_ratio", "score", "weights", "penalty"}
    assert isinstance(data["score"], float)


def test_no_llm_import_in_this_module() -> None:
    """Steps A0 and A3 are deterministic by design. An LLM import here would
    mean the fit score had become a model judgement."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / ("packages/agents/resume_optimizer/prefilter.py")
    tree = ast.parse(source.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            imports |= {m.split(".")[0] for m in mods if m}
    forbidden = {"anthropic", "openai", "httpx", "requests"}
    assert not (imports & forbidden), f"LLM/network import found: {imports & forbidden}"
