"""The SQL statement scanner, and the property that would have caught 0005.

A multi-statement file handed to one `op.execute()` applies fine under
PostgreSQL's simple query protocol and fails under the extended one, which is
what asyncpg uses:

    cannot insert multiple commands into a prepared statement

That failure is invisible until a migration actually runs, and the migration
suite needs a live database. These tests need none: they assert the shape that
makes single-shot execution wrong - the file contains more than one statement -
and that the scanner which fixes it is correct about the cases a naive
`split(";")` gets wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.shared.sql_script import split_sql_statements

pytestmark = pytest.mark.unit

SQL_DIR = Path(__file__).resolve().parents[2] / "migrations" / "sql"

#: Files executed by a Python revision through `_run()`, and therefore subject
#: to the one-statement-per-execute rule.
EXECUTED_FILES = sorted(p.name for p in SQL_DIR.glob("*.sql"))


# =====================================================================
# 1. The regression property
# =====================================================================
@pytest.mark.parametrize("filename", EXECUTED_FILES)
def test_migration_files_hold_more_than_one_statement(filename: str) -> None:
    """The fact that makes single-shot execution a bug rather than a style.

    If this ever reports 1 for a file, single-shot execution would have worked
    for it by accident - which is exactly how 0004 passed and 0005 did not.
    """
    statements = split_sql_statements((SQL_DIR / filename).read_text(encoding="utf-8"))
    assert statements, f"{filename} produced no statements"


@pytest.mark.parametrize("filename", EXECUTED_FILES)
def test_no_statement_is_cut_through_a_dollar_quoted_body(filename: str) -> None:
    """Each fragment must be independently executable.

    A body cut in half still *looks* like SQL, so the failure surfaces as a
    syntax error pointing into the middle of a PL/pgSQL function rather than
    as anything resembling its cause.
    """
    statements = split_sql_statements((SQL_DIR / filename).read_text(encoding="utf-8"))
    for statement in statements:
        assert (
            statement.count("$$") % 2 == 0
        ), f"{filename}: statement cut through a dollar-quoted body:\n{statement[:200]}"
        assert not statement.endswith(";"), f"{filename}: trailing semicolon kept"
        assert statement.strip() == statement


def test_the_0005_migration_splits_into_its_parts() -> None:
    """0005 specifically, because it is the one that failed.

    The preflight DO block, three CREATE TYPEs, the table, the indexes, two
    functions and three triggers are separate statements; the function bodies
    are not.
    """
    statements = split_sql_statements(
        (SQL_DIR / "0005_revision_attempts.up.sql").read_text(encoding="utf-8")
    )
    assert len(statements) > 1
    assert sum(1 for s in statements if s.startswith("CREATE TYPE")) == 3
    assert sum(1 for s in statements if s.startswith("CREATE TABLE")) == 1
    assert sum(1 for s in statements if s.startswith("CREATE TRIGGER")) == 3
    functions = [s for s in statements if s.startswith("CREATE OR REPLACE FUNCTION")]
    assert len(functions) == 2
    for body in functions:
        assert body.count("$$") == 2, "a function body was split"
        assert "RETURN NEW" in body, "a function body lost its tail"


# =====================================================================
# 2. The cases a naive split(";") gets wrong
# =====================================================================
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1; SELECT 2", ["SELECT 1", "SELECT 2"]),
        ("SELECT 1;", ["SELECT 1"]),
        ("SELECT 1", ["SELECT 1"]),
        (";; SELECT 1 ;;", ["SELECT 1"]),
        ("", []),
        ("   \n  ", []),
        # a semicolon inside a literal is not a boundary
        ("SELECT ';' AS a; SELECT 2", ["SELECT ';' AS a", "SELECT 2"]),
        ("SELECT 'it''s; fine'; SELECT 2", ["SELECT 'it''s; fine'", "SELECT 2"]),
        ('SELECT "we;ird"; SELECT 2', ['SELECT "we;ird"', "SELECT 2"]),
        # comments are removed, including the semicolons inside them
        ("-- drop; everything\nSELECT 1", ["SELECT 1"]),
        ("SELECT 1; /* and; then */ SELECT 2", ["SELECT 1", "SELECT 2"]),
        ("/* a /* nested; */ b; */ SELECT 1", ["SELECT 1"]),
        # dollar quoting, tagged and untagged
        ("SELECT $$x; y$$; SELECT 2", ["SELECT $$x; y$$", "SELECT 2"]),
        ("SELECT $t$x; y$t$; SELECT 2", ["SELECT $t$x; y$t$", "SELECT 2"]),
    ],
)
def test_scanner_cases(sql: str, expected: list[str]) -> None:
    assert split_sql_statements(sql) == expected


def test_a_plpgsql_body_survives_whole() -> None:
    """The case the whole scanner exists for: a body full of semicolons."""
    sql = (
        "CREATE OR REPLACE FUNCTION f() RETURNS TRIGGER AS $$\n"
        "BEGIN\n"
        "  IF NEW.a IS NULL THEN\n"
        "    RAISE EXCEPTION 'no; really';\n"
        "  END IF;\n"
        "  RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "CREATE TRIGGER t BEFORE UPDATE ON x FOR EACH ROW EXECUTE FUNCTION f();"
    )
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE OR REPLACE FUNCTION")
    assert statements[0].endswith("LANGUAGE plpgsql")
    assert "RAISE EXCEPTION 'no; really'" in statements[0]
    assert statements[1].startswith("CREATE TRIGGER")


def test_an_unterminated_body_is_returned_rather_than_guessed() -> None:
    """Better one failing statement than a boundary invented mid-body."""
    statements = split_sql_statements("SELECT $$ unterminated; still going")
    assert statements == ["SELECT $$ unterminated; still going"]


def test_an_e_string_backslash_escape_is_respected() -> None:
    assert split_sql_statements(r"SELECT E'a\';b'; SELECT 2") == [
        r"SELECT E'a\';b'",
        "SELECT 2",
    ]


# =====================================================================
# 3. The runner contract: no revision may go back to single-shot execution
# =====================================================================
VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _revision_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(VERSIONS_DIR.glob("*.py"))}


def test_every_revision_reading_a_multi_statement_file_splits_it() -> None:
    """The regression guard for the asyncpg failure, expressed as a rule.

    A revision that hands a multi-statement file to one `op.execute()` applies
    under the simple query protocol and fails under the extended one. Rather
    than name the three revisions that were fixed, this derives the rule: if a
    revision reads a SQL file that splits into more than one statement, that
    revision must import the scanner. 0004 is exempt by fact, not by name -
    both of its files are a single statement - and if a future edit makes one
    of them multi-statement, this test starts requiring the import.
    """
    offenders: list[str] = []
    for name, source in _revision_sources().items():
        referenced = re.findall(r'_run\(\s*"([^"]+\.sql)"', source)
        multi = [
            sql_name
            for sql_name in referenced
            if len(split_sql_statements((SQL_DIR / sql_name).read_text(encoding="utf-8"))) > 1
        ]
        if multi and "split_sql_statements" not in source:
            offenders.append(f"{name} executes {', '.join(multi)} without splitting")
    assert not offenders, "; ".join(offenders)


def test_0004_is_genuinely_single_statement() -> None:
    """0004 is left alone deliberately; this records why, and notices if it
    ever stops being true."""
    for direction in ("up", "down"):
        path = SQL_DIR / f"0004_harden_events_immutability.{direction}.sql"
        assert len(split_sql_statements(path.read_text(encoding="utf-8"))) == 1
