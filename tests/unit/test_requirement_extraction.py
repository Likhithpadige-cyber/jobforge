"""Deterministic requirement extraction (AGENT_DESIGN.md §3.3, step A1)."""

from __future__ import annotations

import pytest

from packages.agents.resume_optimizer.requirements import (
    Necessity,
    RequirementKind,
    extract_requirements,
)

pytestmark = pytest.mark.unit

STRUCTURED = """Cloud Engineer

Requirements:
- 3+ years of hands-on AWS experience
- Strong Linux fundamentals
- Bachelor's degree in Computer Science or equivalent
- AWS Certified Solutions Architect certification

Nice to have:
- Kubernetes exposure
- Terraform in production

Responsibilities:
- Design and maintain cloud infrastructure
- Collaborate with the platform team

Benefits:
- Competitive salary and health insurance
- 401(k) matching
"""


def texts(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [r.text for r in result.requirements]


# --------------------------------------------------------------- structure
def test_extracts_required_and_preferred() -> None:
    result = extract_requirements(STRUCTURED)
    assert len(result.required) >= 4
    assert len(result.preferred) >= 2
    assert any("Kubernetes" in r.text for r in result.preferred)
    assert any("AWS" in r.text for r in result.required)


def test_benefits_section_is_ignored() -> None:
    """Salary and 401(k) are not requirements."""
    result = extract_requirements(STRUCTURED)
    joined = " ".join(texts(result))
    assert "401(k)" not in joined
    assert "health insurance" not in joined


def test_every_requirement_is_a_substring_of_the_posting() -> None:
    """A1 selects and labels. It never rephrases or infers."""
    result = extract_requirements(STRUCTURED)
    for requirement in result.requirements:
        assert requirement.text in STRUCTURED, requirement.text


def test_years_are_parsed() -> None:
    result = extract_requirements(STRUCTURED)
    aws = next(r for r in result.requirements if "AWS experience" in r.text)
    assert aws.years_min == 3


def test_year_range_is_parsed() -> None:
    result = extract_requirements("Requirements:\n- 3-5 years of Python experience\n")
    assert (result.requirements[0].years_min, result.requirements[0].years_max) == (3, 5)


def test_implausible_year_count_is_rejected_not_stored() -> None:
    result = extract_requirements("Requirements:\n- Founded in 1999 years of history\n")
    assert result.requirements[0].years_min is None


# ------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("AWS Certified Solutions Architect certification", RequirementKind.CERTIFICATION),
        ("Bachelor's degree in Computer Science", RequirementKind.EDUCATION),
        ("5 years of experience with Terraform", RequirementKind.EXPERIENCE),
        ("Design and maintain cloud infrastructure", RequirementKind.RESPONSIBILITY),
        ("Proficiency with Python and Bash", RequirementKind.SKILL),
    ],
)
def test_kind_classification(line: str, expected: RequirementKind) -> None:
    result = extract_requirements(f"Requirements:\n- {line}\n")
    assert result.requirements[0].kind is expected


def test_inline_cue_overrides_the_section() -> None:
    """"Kubernetes a plus" inside Requirements is still preferred. Treating
    it as required would manufacture a gap the posting never stated."""
    result = extract_requirements(
        "Requirements:\n- AWS experience\n- Kubernetes is a plus\n"
    )
    kubernetes = next(r for r in result.requirements if "Kubernetes" in r.text)
    assert kubernetes.necessity is Necessity.PREFERRED


def test_required_cue_inside_a_preferred_section() -> None:
    result = extract_requirements(
        "Nice to have:\n- Terraform exposure\n- Must have a valid work permit\n"
    )
    permit = next(r for r in result.requirements if "work permit" in r.text)
    assert permit.necessity is Necessity.REQUIRED


def test_boilerplate_is_dropped() -> None:
    result = extract_requirements(
        "Requirements:\n- 3 years AWS\n"
        "- We are an equal opportunity employer\n"
        "- Subject to a background check\n"
    )
    assert len(result.requirements) == 1


# ---------------------------------------------------------------- confidence
def test_structured_posting_scores_high_confidence() -> None:
    result = extract_requirements(STRUCTURED)
    assert result.confidence >= 0.7
    assert result.needs_llm_fallback is False


def test_unstructured_prose_flags_for_llm_fallback() -> None:
    """Low confidence is not a failure - it is the signal that A1 should
    defer to a model, exactly as the design specifies."""
    prose = (
        "We are looking for a talented cloud engineer to join our growing team. "
        "You should be comfortable with AWS and enjoy solving hard problems in a "
        "fast-paced environment alongside a supportive group of colleagues."
    )
    result = extract_requirements(prose)
    assert result.needs_llm_fallback is True
    assert result.warnings


def test_empty_description() -> None:
    result = extract_requirements("")
    assert result.requirements == []
    assert result.confidence == 0.0
    assert result.needs_llm_fallback is True


def test_none_description() -> None:
    assert extract_requirements(None).needs_llm_fallback is True


def test_no_requirements_extracted_is_never_silent() -> None:
    """An empty list would make every job look like a perfect match to A3,
    so it must always carry a warning and a fallback flag."""
    result = extract_requirements("About us:\nWe make software.\n")
    assert result.requirements == []
    assert result.warnings
    assert result.needs_llm_fallback is True


def test_serialisable_for_the_evaluation_row() -> None:
    data = extract_requirements(STRUCTURED).as_dict()
    assert set(data) >= {"requirements", "confidence", "needs_llm_fallback", "counts"}
    assert data["counts"]["total"] == len(data["requirements"])  # type: ignore[index]


def test_deterministic() -> None:
    runs = {
        tuple(r.text for r in extract_requirements(STRUCTURED).requirements)
        for _ in range(20)
    }
    assert len(runs) == 1


def test_no_llm_import() -> None:
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "packages/agents/resume_optimizer/requirements.py"
    )
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import | ast.ImportFrom):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            imports |= {m.split(".")[0] for m in mods if m}
    assert not (imports & {"anthropic", "openai", "httpx", "requests"})


def test_responsibilities_are_not_required_qualifications() -> None:
    """A responsibility describes the job, not something the candidate must
    already have done. Marking it required would make A3 score it as an unmet
    gap and penalise the candidate for not having already held the role."""
    result = extract_requirements(
        "Responsibilities:\n"
        "- Design and maintain cloud infrastructure\n"
        "- Collaborate with the platform team\n"
    )
    assert result.requirements
    assert all(r.kind is RequirementKind.RESPONSIBILITY for r in result.requirements)
    assert all(r.necessity is Necessity.PREFERRED for r in result.requirements)


def test_explicit_must_have_promotes_a_responsibility() -> None:
    result = extract_requirements(
        "Responsibilities:\n- Must have on-call availability one week in four\n"
    )
    assert result.requirements[0].necessity is Necessity.REQUIRED
