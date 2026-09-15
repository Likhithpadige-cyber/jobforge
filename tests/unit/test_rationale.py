"""A4 — deterministic rationale. Pure: no database, no model, no network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from packages.agents.resume_optimizer.rationale import (
    MAX_NAMED_ITEMS,
    build_rationale,
    rationale_from_breakdown,
    rationale_text,
)

pytestmark = pytest.mark.unit


def row(text: str, *, necessity="required", status="met", facts=("f1",),
        assessable=True, years_required=None, years_evidenced=None):
    return {
        "requirement_text": text,
        "necessity": necessity,
        "kind": "skill",
        "status": status,
        "fact_ids": list(facts),
        "assessable": assessable,
        "years_required": years_required,
        "years_evidenced": years_evidenced,
    }


def matrix(*rows):
    return {"requirements": list(rows)}


# ------------------------------------------------------------- determinism
def test_same_input_always_produces_the_same_text() -> None:
    data = matrix(row("AWS"), row("Kubernetes", status="not_met", facts=()))
    texts = {
        build_rationale(data, score=72, recommendation="moderate").summary
        for _ in range(30)
    }
    assert len(texts) == 1


def test_sentence_order_is_fixed() -> None:
    data = matrix(row("AWS"), row("Kubernetes", status="not_met", facts=()))
    result = build_rationale(data, score=72, recommendation="moderate")
    summary = result.summary
    assert summary.index(result.headline) == 0
    if result.strengths:
        assert summary.index(result.strengths[0]) < summary.index(result.gaps[0])


def test_no_llm_or_network_import() -> None:
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "packages/agents/resume_optimizer/rationale.py"
    )
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import | ast.ImportFrom):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            imports |= {m.split(".")[0] for m in mods if m}
    assert not (imports & {"anthropic", "openai", "httpx", "requests", "random"})


# ---------------------------------------------------------------- content
def test_headline_states_the_band_and_score() -> None:
    result = build_rationale(matrix(row("AWS")), score=87, recommendation="strong")
    assert "Strong match" in result.headline
    assert "87" in result.headline


def test_headline_counts_covered_essentials() -> None:
    data = matrix(row("AWS"), row("Linux"), row("K8s", status="not_met", facts=()))
    result = build_rationale(data, score=70, recommendation="moderate")
    assert "2 of 3 essential requirements" in result.headline


def test_met_requirements_are_named() -> None:
    result = build_rationale(matrix(row("AWS")), score=87, recommendation="strong")
    assert any("AWS" in s for s in result.strengths)


def test_unmet_essentials_are_always_named() -> None:
    data = matrix(row("Kubernetes", status="not_met", facts=()))
    result = build_rationale(data, score=30, recommendation="no")
    assert any("Kubernetes" in g for g in result.gaps)


def test_nothing_missing_is_stated_positively() -> None:
    result = build_rationale(matrix(row("AWS")), score=95, recommendation="strong")
    assert any("Nothing essential is missing" in g for g in result.gaps)


def test_long_lists_are_bounded() -> None:
    data = matrix(*[row(f"Skill {i}") for i in range(10)])
    result = build_rationale(data, score=90, recommendation="strong")
    named = result.strengths[0]
    assert named.count(",") <= MAX_NAMED_ITEMS
    assert len(named) < 300


# -------------------------------------------------------------- honesty
@pytest.mark.safety
def test_uncited_met_row_is_not_reported_as_covered() -> None:
    """A row claiming `met` with no fact behind it scores zero in A3. The
    prose must not disagree with the score."""
    data = matrix(row("AWS", status="met", facts=()))
    result = build_rationale(data, score=40, recommendation="weak")
    assert not any("Fully covered" in s for s in result.strengths)
    assert any("AWS" in g for g in result.gaps)
    assert result.counts["required_met"] == 0


@pytest.mark.safety
def test_unassessable_rows_are_declared_not_hidden() -> None:
    data = matrix(row("Good attitude", status="not_met", facts=(), assessable=False))
    result = build_rationale(data, score=50, recommendation="weak")
    assert any("could not be checked" in c for c in result.caveats)


@pytest.mark.safety
def test_unassessable_rows_are_not_listed_as_candidate_failings() -> None:
    """"Could not be checked" is a limit of the matcher, not a gap in the
    person. It must not be named alongside real unmet requirements."""
    data = matrix(row("Good attitude", status="not_met", facts=(), assessable=False))
    result = build_rationale(data, score=50, recommendation="weak")
    assert not any("Good attitude" in g for g in result.gaps)


@pytest.mark.safety
def test_years_shortfall_is_declared() -> None:
    data = matrix(row("3+ years AWS", status="partially_met",
                      years_required=3, years_evidenced=0.5))
    result = build_rationale(data, score=60, recommendation="moderate")
    assert any("more years than your verified facts evidence" in c
               for c in result.caveats)


@pytest.mark.safety
def test_no_supporting_facts_at_all_is_stated_plainly() -> None:
    data = matrix(row("AWS", status="not_met", facts=()))
    result = build_rationale(data, score=20, recommendation="no")
    assert any("No verified fact supported any requirement" in c
               for c in result.caveats)


@pytest.mark.safety
def test_rationale_invents_no_requirement_text() -> None:
    data = matrix(row("AWS"), row("Kubernetes", status="not_met", facts=()))
    result = build_rationale(data, score=70, recommendation="moderate")
    for sentence in (*result.strengths, *result.gaps):
        for word in ("Terraform", "Docker", "PhD"):
            assert word not in sentence


@pytest.mark.safety
def test_rationale_never_names_a_fact_id_as_prose() -> None:
    data = matrix(row("AWS", facts=("00000000-0000-7000-8000-000000000001",)))
    result = build_rationale(data, score=87, recommendation="strong")
    assert "00000000" not in result.summary


# ----------------------------------------------------------------- edges
def test_empty_matrix_is_explained_rather_than_left_blank() -> None:
    result = build_rationale({}, score=55, recommendation="weak")
    assert "No requirements could be read" in result.headline
    assert result.summary


def test_none_matrix_is_handled() -> None:
    assert build_rationale(None, score=50, recommendation="weak").summary


def test_matrix_without_requirements_key() -> None:
    assert build_rationale({"summary": {}}, score=50, recommendation="weak").summary


def test_malformed_rows_do_not_raise() -> None:
    result = build_rationale({"requirements": [{}, {"status": "met"}]},
                             score=50, recommendation="weak")
    assert isinstance(result.summary, str)


def test_counts_are_internally_consistent() -> None:
    data = matrix(row("AWS"), row("Linux", status="partially_met"),
                  row("K8s", status="not_met", facts=()),
                  row("Terraform", necessity="preferred"))
    counts = build_rationale(data, score=70, recommendation="moderate").counts
    assert counts["required"] == 3
    assert counts["preferred"] == 1
    assert counts["required_met"] + counts["required_partial"] \
        + counts["required_unmet"] == counts["required"]


def test_extra_gaps_from_a3_are_named_verbatim() -> None:
    result = build_rationale(matrix(row("AWS")), score=70,
                             recommendation="moderate", gaps=["Security clearance"])
    assert any("Security clearance" in g for g in result.gaps)


def test_a3_gaps_already_in_the_matrix_are_not_repeated() -> None:
    data = matrix(row("Kubernetes", status="not_met", facts=()))
    result = build_rationale(data, score=40, recommendation="weak",
                             gaps=["Kubernetes"])
    assert sum("Kubernetes" in g for g in result.gaps) == 1


def test_as_dict_is_serialisable_and_labelled_deterministic() -> None:
    data = build_rationale(matrix(row("AWS")), score=87,
                           recommendation="strong").as_dict()
    assert data["method"] == "deterministic"
    assert set(data) >= {"headline", "strengths", "gaps", "caveats", "counts", "summary"}


# --------------------------------------------------------- A3 / A5 handoff
def test_rationale_from_breakdown_uses_the_score_object() -> None:
    from packages.agents.resume_optimizer.prefilter import (
        MatchStatus,
        RequirementMatch,
        compute_score,
    )

    breakdown = compute_score([RequirementMatch("AWS", True, MatchStatus.MET, ["f1"])])
    result = rationale_from_breakdown(matrix(row("AWS")), breakdown)
    assert str(breakdown.score.quantize(Decimal("1"))) in result.headline \
        or f"{breakdown.score:g}" in result.headline


def test_rationale_text_returns_the_string_a5_stores() -> None:
    from packages.agents.resume_optimizer.prefilter import (
        MatchStatus,
        RequirementMatch,
        compute_score,
    )

    breakdown = compute_score([RequirementMatch("AWS", True, MatchStatus.MET, ["f1"])])
    text = rationale_text(matrix(row("AWS")), breakdown)
    assert isinstance(text, str)
    assert text.strip()
    assert len(text) < 1200, "must fit comfortably in a TEXT column and be readable"
