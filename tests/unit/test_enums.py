"""Enum definitions must match the design and the migration exactly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.core.models.enums import (
    ENUM_DEFINITIONS,
    HUMAN_APPROVAL_REQUIRED_STATUSES,
    HUMAN_ONLY_QUESTION_CLASSES,
    ApplicationStatus,
    QuestionClass,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _sql_enums() -> dict[str, list[str]]:
    """Every enum the migrations create, in declaration order, merged.

    Scans all `*.up.sql` rather than naming one file. A hardcoded 0002 could
    not see the types 0005 adds, so `test_every_python_enum_exists_in_sql`
    reported them as missing from SQL when they were present - the test was
    looking in the wrong place, not finding a real gap. Every later migration
    that adds a type would have hit the same wall.

    A duplicate definition fails loudly rather than letting the last file win:
    two `CREATE TYPE` statements for one name cannot both apply, so silently
    overwriting would make this function disagree with the database it is
    supposed to describe.
    """
    enums: dict[str, list[str]] = {}
    for path in sorted((ROOT / "migrations" / "sql").glob("*.up.sql")):
        sql = path.read_text()
        for match in re.finditer(
            r"^CREATE TYPE (\w+) AS ENUM \((.*?)\);", sql, re.DOTALL | re.MULTILINE
        ):
            name = match.group(1)
            assert name not in enums, (
                f"duplicate enum definition: {name} is created in "
                f"{path.name} and in an earlier migration"
            )
            enums[name] = re.findall(r"'([^']+)'", match.group(2))
    return enums


def test_every_python_enum_exists_in_sql() -> None:
    sql = _sql_enums()
    assert set(ENUM_DEFINITIONS) == set(sql)


@pytest.mark.parametrize("name", sorted(ENUM_DEFINITIONS))
def test_enum_values_and_order_match_sql(name: str) -> None:
    assert ENUM_DEFINITIONS[name] == _sql_enums()[name]


def test_application_status_covers_the_state_machine() -> None:
    """ARCHITECTURE.md §7.2. Missing a status would leave the state machine
    unable to represent a real state."""
    required = {
        "created", "resume_generating", "resume_ready", "verification_failed",
        "questions_pending", "awaiting_approval", "changes_requested", "approved",
        "submission_ready", "submitting", "submitted", "submission_failed",
        "manual_handoff", "policy_blocked", "abandoned", "error",
    }
    assert {s.value for s in ApplicationStatus} == required


@pytest.mark.safety
def test_approval_required_statuses_match_the_check_constraint() -> None:
    """The Python set and the SQL CHECK must agree, or one of them is a lie."""
    sql = (ROOT / "migrations" / "sql" / "0002_core_schema.up.sql").read_text()
    match = re.search(
        r"ck_applications_approved_requires_human CHECK \(\s*status NOT IN \(([^)]+)\)",
        sql,
    )
    assert match, "approval CHECK constraint not found"
    in_sql = set(re.findall(r"'([^']+)'", match.group(1)))
    assert in_sql == {s.value for s in HUMAN_APPROVAL_REQUIRED_STATUSES}


@pytest.mark.safety
def test_human_only_question_classes_exclude_auto_answerable_ones() -> None:
    """AGENT_DESIGN.md §4.2: sensitive and judgement questions are human-only."""
    assert QuestionClass.SENSITIVE in HUMAN_ONLY_QUESTION_CLASSES
    assert QuestionClass.JUDGEMENT in HUMAN_ONLY_QUESTION_CLASSES
    assert QuestionClass.UNCERTAIN in HUMAN_ONLY_QUESTION_CLASSES
    assert QuestionClass.UNANSWERABLE in HUMAN_ONLY_QUESTION_CLASSES
    assert QuestionClass.FACTUAL_SAFE not in HUMAN_ONLY_QUESTION_CLASSES
    assert QuestionClass.DERIVED not in HUMAN_ONLY_QUESTION_CLASSES
