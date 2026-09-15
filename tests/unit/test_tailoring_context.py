"""B0 matrix validation is pure and fails closed."""
from copy import deepcopy
from uuid import uuid4

import pytest

from packages.agents.resume_optimizer.context import (
    MalformedMatchMatrixError, validate_match_matrix,
)
from packages.agents.resume_optimizer.evidence import FactView, build_match_matrix
from packages.agents.resume_optimizer.requirements import Necessity, Requirement, RequirementKind


def matrix():
    fact = FactView(uuid4(), "AWS")
    result = build_match_matrix(
        [Requirement(text="AWS", necessity=Necessity.REQUIRED, kind=RequirementKind.SKILL)],
        [fact],
    ).as_dict()
    return result, fact.id


def test_valid_matrix_is_not_modified():
    value, fact_id = matrix()
    before = deepcopy(value)
    validate_match_matrix(value, {fact_id})
    assert value == before


def test_empty_requirements_are_valid():
    validate_match_matrix({"requirements": []}, set())


@pytest.mark.parametrize("value", [None, [], {}, {"requirements": None}, {"requirements": {}}, {"requirements": [None]}])
def test_malformed_container(value):
    with pytest.raises(MalformedMatchMatrixError):
        validate_match_matrix(value, set())


@pytest.mark.parametrize("field,value", [
    ("requirement_text", ""), ("requirement_text", 1),
    ("status", "invented"), ("necessity", "optional"), ("kind", "invented"),
    ("fact_ids", None), ("fact_ids", [1]), ("fact_ids", ["bad-uuid"]),
    ("fact_ids", []), ("evidence", {}), ("evidence", [{}]),
    ("years_required", True), ("years_required", -1), ("years_evidenced", "3"),
    ("matched_terms", "AWS"), ("unmatched_terms", [3]), ("assessable", "yes"),
])
def test_malformed_row(field, value):
    payload, fact_id = matrix()
    payload["requirements"][0][field] = value
    with pytest.raises(MalformedMatchMatrixError):
        validate_match_matrix(payload, {fact_id})


def test_unavailable_citation_is_rejected():
    payload, _ = matrix()
    with pytest.raises(MalformedMatchMatrixError):
        validate_match_matrix(payload, set())
