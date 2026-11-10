#!/usr/bin/env python3
"""Verify the Phase 2 schema three ways, without a database.

Compares, by AST and by SQL parsing:

  models   packages/core/models/*.py
  migration migrations/sql/0002_core_schema.up.sql
  design   docs/DATABASE_DESIGN.md

Checks performed:

  1. every table in the design exists in the models and in the migration
  2. no table exists in the models that is absent from the migration
  3. every column of every model appears in the migration's CREATE TABLE
  4. no column exists in the migration that is absent from the model
  5. every enum type and its exact value list matches between the three
  6. the migration's FK targets all exist and are created beforehand
  7. constraints named in the design are present in the migration
  8. security invariants: grant statements, triggers, append-only rules
  9. the down migration reverses every table, type and function

This is the closest thing to `alembic upgrade head` that runs with no
PostgreSQL, no SQLAlchemy and no network.

    python3 scripts/verify_schema.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "packages" / "core" / "models"
SQL_DIR = ROOT / "migrations" / "sql"
DESIGN = ROOT / "docs" / "DATABASE_DESIGN.md"


class SqlFiles:
    """One or more schema-bearing SQL files, read as a single document.

    Every check below was written against a single file. Rather than thread a
    list through all of them, this presents the concatenation with the same
    `read_text()` interface, so adding a migration is one entry here instead
    of an edit in nine places. Order matters: FK-ordering and down-migration
    checks read the text positionally, so files are listed in apply order.
    """

    def __init__(self, *names: str) -> None:
        self.paths = [SQL_DIR / name for name in names]

    def read_text(self, encoding: str = "utf-8") -> str:
        return "\n".join(path.read_text(encoding=encoding) for path in self.paths)

    def missing(self) -> list[str]:
        return [p.name for p in self.paths if not p.is_file()]


#: Schema-bearing migrations, in apply order. 0003 and the 0005 grants file
#: carry privileges rather than schema and are read through GRANTS_SQL.
UP_SQL = SqlFiles(
    "0002_core_schema.up.sql",
    "0005_revision_attempts.up.sql",
    "0006_resume_rework_limit.up.sql",
    "0007_evaluation_assessment.up.sql",
    "0008_artifact_binding.up.sql",
    "0009_application_preferences.up.sql",
    "0010_submission_retry_schedule.up.sql",
    "0011_application_tracking.up.sql",
)
DOWN_SQL = SqlFiles(
    "0002_core_schema.down.sql",
    "0005_revision_attempts.down.sql",
    "0006_resume_rework_limit.down.sql",
    "0007_evaluation_assessment.down.sql",
    "0008_artifact_binding.down.sql",
    "0009_application_preferences.down.sql",
    "0010_submission_retry_schedule.down.sql",
    "0011_application_tracking.down.sql",
)
GRANTS_SQL = SqlFiles("0003_roles_grants.up.sql", "0005_revision_attempts.grants.up.sql")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))


# --------------------------------------------------------------- model side
def model_tables() -> dict[str, set[str]]:
    """{table_name: {column names}} extracted from the ORM classes."""
    tables: dict[str, set[str]] = {}

    for path in sorted(MODELS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            tablename: str | None = None
            columns: set[str] = set()
            bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}

            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets
                    )
                    and isinstance(stmt.value, ast.Constant)
                ):
                    tablename = stmt.value.value
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    # Only mapped_column(...) assignments are real columns;
                    # relationship() and bare annotations are not.
                    value = stmt.value
                    is_column = (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "mapped_column"
                    ) or value is None
                    if is_column:
                        columns.add(stmt.target.id)

            if tablename:
                if "TimestampMixin" in bases:
                    columns |= {"created_at", "updated_at"}
                if "UUIDPrimaryKeyMixin" in bases:
                    columns.add("id")
                tables[tablename] = columns
    return tables


def model_enums() -> dict[str, list[str]]:
    """Enum name -> values, read from ENUM_DEFINITIONS in enums.py."""
    tree = ast.parse((MODELS_DIR / "enums.py").read_text(encoding="utf-8"))
    classes: dict[str, list[str]] = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        values = [
            stmt.value.value
            for stmt in cls.body
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ]
        if values:
            classes[cls.name] = values

    # Map PostgreSQL type name -> python class via the _pg(...) calls.
    mapping: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_pg"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[1], ast.Name)
        ):
            pg_name = node.args[0].value
            cls_name = node.args[1].id
            if cls_name in classes:
                mapping[pg_name] = classes[cls_name]
    return mapping


# ------------------------------------------------------------- migration side
def sql_tables() -> dict[str, set[str]]:
    """{table: {columns}} parsed from CREATE TABLE blocks."""
    sql = UP_SQL.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"^CREATE TABLE (\w+) \((.*?)^\);", sql, re.MULTILINE | re.DOTALL):
        name, body = match.group(1), match.group(2)
        columns: set[str] = set()

        # Strip comments, then split on commas that are not inside parentheses.
        cleaned = "\n".join(line for line in body.splitlines() if not line.strip().startswith("--"))
        statements: list[str] = []
        depth = 0
        current: list[str] = []
        for char in cleaned:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                statements.append("".join(current))
                current = []
            else:
                current.append(char)
        statements.append("".join(current))

        for statement in statements:
            token = statement.strip()
            if not token:
                continue
            first = token.split()[0]
            if first.upper() in {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}:
                continue
            if re.fullmatch(r"\w+", first):
                columns.add(first)
        tables[name] = columns
    # Schema evolution may add columns after the original CREATE TABLE.
    for table, column in re.findall(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)", sql, re.I):
        tables.setdefault(table, set()).add(column)
    return tables


def sql_enums() -> dict[str, list[str]]:
    sql = UP_SQL.read_text(encoding="utf-8")
    enums: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TYPE (\w+) AS ENUM \((.*?)\);", sql, re.DOTALL):
        values = re.findall(r"'([^']+)'", match.group(2))
        enums[match.group(1)] = values
    return enums


# ---------------------------------------------------------------- design side
def design_tables() -> set[str]:
    text = DESIGN.read_text(encoding="utf-8")
    from_headings = set(re.findall(r"^### \d+\.\d+ `(\w+)`", text, re.MULTILINE))
    from_ddl = set(re.findall(r"^CREATE TABLE (\w+)", text, re.MULTILINE))
    # Tables the design describes in prose inside a combined section.
    prose = {
        "educations",
        "certifications",
        "skills",
        "projects",
        "cover_letters",
        "cover_letter_claims",
        "application_answers",
        "agent_run_steps",
        "llm_calls",
    }
    return from_headings | from_ddl | prose


def design_enums() -> dict[str, list[str]]:
    text = DESIGN.read_text(encoding="utf-8")
    enums: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TYPE (\w+) AS ENUM \((.*?)\);", text, re.DOTALL):
        enums[match.group(1)] = re.findall(r"'([^']+)'", match.group(2))
    return enums


# --------------------------------------------------------------------- checks
def check_tables(models: dict[str, set[str]], sql: dict[str, set[str]]) -> None:
    design = design_tables()

    missing_from_models = sorted(design - set(models))
    if missing_from_models:
        record(FAIL, "design tables exist as models", ", ".join(missing_from_models))
    else:
        record(PASS, "design tables exist as models", f"{len(design)} tables")

    missing_from_sql = sorted(set(models) - set(sql))
    if missing_from_sql:
        record(FAIL, "model tables exist in migration", ", ".join(missing_from_sql))
    else:
        record(PASS, "model tables exist in migration", f"{len(models)} tables")

    extra_in_sql = sorted(set(sql) - set(models))
    if extra_in_sql:
        record(FAIL, "migration has no orphan tables", ", ".join(extra_in_sql))
    else:
        record(PASS, "migration has no orphan tables", "")


def check_columns(models: dict[str, set[str]], sql: dict[str, set[str]]) -> None:
    problems: list[str] = []
    compared = 0
    for table, model_cols in sorted(models.items()):
        if table not in sql:
            continue
        sql_cols = sql[table]
        compared += len(model_cols)
        only_model = sorted(model_cols - sql_cols)
        only_sql = sorted(sql_cols - model_cols)
        if only_model:
            problems.append(f"{table}: in model not migration: {', '.join(only_model)}")
        if only_sql:
            problems.append(f"{table}: in migration not model: {', '.join(only_sql)}")
    if problems:
        record(FAIL, "columns match model <-> migration", " | ".join(problems[:6]))
    else:
        record(PASS, "columns match model <-> migration", f"{compared} columns")


def check_enums() -> None:
    models, sql, design = model_enums(), sql_enums(), design_enums()
    problems: list[str] = []

    for name, values in sorted(design.items()):
        if name not in sql:
            problems.append(f"{name} missing from migration")
        elif sql[name] != values:
            problems.append(f"{name} values differ from design")
        if name not in models:
            problems.append(f"{name} missing from models")
        elif models[name] != values:
            problems.append(f"{name} values differ from models")

    extra = sorted(set(sql) - set(design))
    if extra:
        problems.append(f"migration has enums not in design: {', '.join(extra)}")

    if problems:
        record(FAIL, "enum types match all three", "; ".join(problems[:6]))
    else:
        record(PASS, "enum types match all three", f"{len(design)} enums")


def check_fk_order() -> None:
    """Every FK target must be created before the referencing table."""
    sql = UP_SQL.read_text(encoding="utf-8")
    order = re.findall(r"^CREATE TABLE (\w+)", sql, re.MULTILINE)
    position = {name: i for i, name in enumerate(order)}
    problems: list[str] = []
    checked = 0

    for match in re.finditer(r"^CREATE TABLE (\w+) \((.*?)^\);", sql, re.MULTILINE | re.DOTALL):
        table, body = match.group(1), match.group(2)
        for fk in re.finditer(r"REFERENCES (\w+) ", body):
            target = fk.group(1)
            checked += 1
            if target not in position:
                problems.append(f"{table} -> unknown table {target}")
            elif position[target] > position[table] and target != table:
                problems.append(f"{table} references {target} before it is created")

    # The deferred cycle FKs are added by ALTER after both tables exist.
    deferred = re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT \w+\s+FOREIGN KEY", sql)
    if problems:
        record(FAIL, "FK targets created in order", "; ".join(problems[:5]))
    else:
        record(
            PASS,
            "FK targets created in order",
            f"{checked} inline + {len(deferred)} deferred (cycle)",
        )


def check_design_constraints() -> None:
    """The constraints the design calls out by name must exist."""
    sql = UP_SQL.read_text(encoding="utf-8")
    required = {
        "ck_applications_approved_requires_human": "human-approval invariant",
        "ck_applications_submitted_requires_timestamp": "submitted timestamp",
        "ck_resume_claims_claim_has_facts": "no uncited claim",
        "ck_cover_letter_claims_claim_has_facts": "no uncited cover-letter claim",
        "ck_approvals_decided_has_actor": "approval names a decider",
        "ck_application_answers_sensitive_human_only": "sensitive answers human-only",
        "uq_application_per_job": "one live application per job",
        "uq_one_pending_approval": "one pending approval per kind",
        "uq_facts_source": "fact uniqueness by source",
        "uq_jobs_source_external": "job dedupe by source id",
        "ck_profile_facts_verified_requires_actor": "verified fact names a verifier",
        # 0005 (C3_HARDENING_DESIGN.md A.3). Names follow the repo's
        # NAMING_CONVENTION (ck_%(table_name)s_%(constraint_name)s), not the
        # design's ck_attempt_* shorthand, so the ORM and the DDL agree.
        "uq_revision_attempt_per_approval": "one live attempt per approval",
        "uq_resume_version_per_application": "resume version allocation backstop",
        "ck_resume_revision_attempts_outcome_only_on_success": (
            "a semantic negative is never a failed status"
        ),
        "ck_resume_revision_attempts_succeeded_requires_result": "a succeeded attempt has a result",
        "ck_resume_revision_attempts_failed_requires_class": "a failed attempt is classified",
        "ck_resume_revision_attempts_open_has_no_result": "an open attempt carries no result",
        "ck_resume_revision_attempts_abandoned_named_a_human": "abandonment names a human",
        "ck_resume_revision_attempts_running_has_lease": "a running attempt holds a lease",
        "ck_resume_revision_attempts_evidence_stale_retry_coherent": (
            "retry counter and its timestamp agree"
        ),
        # 0009 (M9). An agent-authored answer must cite at least one fact.
        "ck_application_answers_agent_requires_facts": "no uncited agent answer",
    }
    missing = [f"{n} ({d})" for n, d in required.items() if n not in sql]
    if missing:
        record(FAIL, "design constraints present", "; ".join(missing))
    else:
        record(PASS, "design constraints present", f"{len(required)} constraints")


def check_security_invariants() -> None:
    sql = UP_SQL.read_text(encoding="utf-8")
    grants_raw = GRANTS_SQL.read_text(encoding="utf-8")
    # Comments explain the rules; only executable SQL proves them.
    grants = "\n".join(
        line for line in grants_raw.splitlines() if not line.strip().startswith("--")
    )
    problems: list[str] = []

    # Agents must not hold write access to the fact tables.
    revoke_block = re.search(
        r"REVOKE INSERT, UPDATE, DELETE ON(.*?)FROM app_agent;", grants, re.DOTALL
    )
    if not revoke_block:
        problems.append("no REVOKE ... FROM app_agent block")
    else:
        revoked = revoke_block.group(1)
        for table in (
            "profile_facts",
            "profiles",
            "site_policies",
            "settings",
            "users",
            "approvals",
        ):
            if table not in revoked:
                problems.append(f"app_agent write access to {table} not revoked")

    if re.search(
        r"GRANT[^;]*\bON\b[^;]*\bprofile_facts\b[^;]*TO[^;]*app_agent", grants.replace("\n", " ")
    ):
        granted = re.findall(
            r"GRANT ([A-Z, ()a-z_]+) ON ([^;]*?) TO ([^;]*?);", grants.replace("\n", " ")
        )
        for privileges, tables, roles in granted:
            if "profile_facts" in tables and "app_agent" in roles and "SELECT" not in privileges:
                problems.append("app_agent has non-SELECT grant on profile_facts")

    # The events guard must compare the whole row, not an enumerated column
    # list - an enumeration silently stops protecting any column added later.
    hardening = ROOT / "migrations" / "sql" / "0004_harden_events_immutability.up.sql"
    if not hardening.is_file():
        problems.append("missing 0004 events-immutability hardening")
    else:
        hardened = hardening.read_text(encoding="utf-8")
        if "to_jsonb(OLD)" not in hardened or "to_jsonb(NEW)" not in hardened:
            problems.append("events guard does not compare the whole row")
        if "- 'published' - 'published_at'" not in hardened:
            problems.append("events guard does not exempt publication columns")

    # The approval-forging gap must be closed by the trigger.
    if "trg_applications_require_real_approval" not in sql:
        problems.append("missing trg_applications_require_real_approval")
    if "jobforge_check_claim_facts" not in sql:
        problems.append("missing claim -> verified fact trigger")
    if "jobforge_protect_verified_facts" not in sql:
        problems.append("missing verified-fact immutability trigger")

    # Audit trail must be undeletable.
    if (
        "REVOKE DELETE ON events, audit_logs, application_status_history FROM PUBLIC"
        not in grants.replace("\n  ", " ").replace("\n", " ")
    ):
        problems.append("audit trail DELETE not revoked from PUBLIC")

    # Agents must not be able to create objects to work around grants.
    if "REVOKE CREATE ON SCHEMA public FROM PUBLIC" not in grants:
        problems.append("CREATE on schema public not revoked")

    # No role may re-grant its privileges.
    if "WITH GRANT OPTION" in grants:
        problems.append("WITH GRANT OPTION present: a role could widen access")

    if problems:
        record(FAIL, "security invariants in DDL", "; ".join(problems[:6]))
    else:
        record(PASS, "security invariants in DDL", "grants + 3 triggers + append-only")


def check_audit_role_containment() -> None:
    """The static half of the app_audit privilege gate (H.5, I.6.6).

    `information_schema.role_table_grants` is the wrong instrument and this
    script has no database anyway. What it CAN prove is that the migration
    diff cannot widen the role: every grant is in the matrix, every UPDATE is
    column-scoped with the exact column list, and no blanket or inherited
    privilege is introduced. The effective boundary - what the cluster
    actually resolves after PUBLIC, memberships and defaults - is established
    at runtime by the integration tests, which is a different claim requiring
    a different instrument. Neither half is sufficient alone.
    """
    grants_raw = GRANTS_SQL.read_text(encoding="utf-8")
    grants = "\n".join(
        line for line in grants_raw.splitlines() if not line.strip().startswith("--")
    )
    flat = re.sub(r"\s+", " ", grants)
    problems: list[str] = []

    #: The matrix of I.6.1. Table -> (row privileges, column-scoped UPDATE).
    matrix: dict[str, tuple[set[str], tuple[str, ...]]] = {
        "agent_runs": (
            {"SELECT", "INSERT"},
            (
                "status",
                "steps_completed",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "prompt_version",
                "error_type",
                "error_message",
                "duration_ms",
                "finished_at",
            ),
        ),
        "agent_run_steps": (
            {"SELECT", "INSERT"},
            ("status", "input_digest", "output", "guardrail_results", "duration_ms", "finished_at"),
        ),
        "llm_calls": ({"SELECT", "INSERT"}, ("response_redacted",)),
        "events": ({"SELECT", "INSERT"}, ()),
    }

    for statement in re.findall(r"GRANT ([^;]+?) TO ([^;]+?);", flat):
        privileges, roles = statement
        if "app_audit" not in roles:
            continue
        if " ON " not in privileges:
            continue
        head, _, target = privileges.rpartition(" ON ")
        target = target.strip()
        if target == "SCHEMA public":
            if head.strip().upper() != "USAGE":
                problems.append(f"app_audit holds {head.strip()} on schema public")
            continue
        if re.search(r"ALL (TABLES|SEQUENCES|FUNCTIONS) IN SCHEMA", privileges, re.I):
            problems.append("blanket GRANT ... ON ALL ... IN SCHEMA to app_audit")
            continue
        if target not in matrix:
            problems.append(f"app_audit granted on {target}, which is not in the matrix")
            continue

        allowed_row, allowed_cols = matrix[target]
        update_cols = re.search(r"UPDATE\s*\(([^)]*)\)", head, re.I)
        if update_cols:
            got = tuple(c.strip() for c in update_cols.group(1).split(",") if c.strip())
            if got != allowed_cols:
                problems.append(f"{target}: UPDATE columns {got} != matrix {allowed_cols}")
        else:
            for word in re.split(r"[,\s]+", head.strip()):
                privilege = word.upper()
                if not privilege:
                    continue
                if privilege == "UPDATE":
                    problems.append(
                        f"{target}: bare GRANT UPDATE to app_audit; the matrix "
                        "requires a column list"
                    )
                elif privilege not in allowed_row:
                    # DELETE, TRUNCATE, REFERENCES and TRIGGER need no separate
                    # branch: `allowed_row` only ever holds SELECT and INSERT,
                    # so anything dangerous fails this test by construction.
                    problems.append(f"{target}: app_audit granted {privilege}")

    # Role shape. NOINHERIT matters: it is what stops a membership added by
    # mistake from conferring privileges implicitly.
    if "CREATE ROLE app_audit LOGIN NOINHERIT" not in flat:
        problems.append("app_audit is not created LOGIN NOINHERIT")
    for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOBYPASSRLS"):
        if attribute not in flat:
            problems.append(f"app_audit is not explicitly {attribute}")
    if re.search(r"GRANT (app_api|app_agent|app_monitor|app_migrate) TO app_audit", flat):
        problems.append("app_audit is granted membership of another role")
    if re.search(r"GRANT app_audit TO ", flat):
        problems.append("another role is granted membership of app_audit")
    if re.search(r"ALTER DEFAULT PRIVILEGES[^;]*app_audit", flat):
        problems.append("ALTER DEFAULT PRIVILEGES names app_audit")

    # Default-deny prologue must exist and precede every grant to the role.
    revoke_all = flat.find("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_audit")
    first_grant = flat.find("TO app_audit")
    if revoke_all < 0:
        problems.append("no REVOKE ALL ON ALL TABLES ... FROM app_audit prologue")
    elif 0 <= first_grant < revoke_all:
        problems.append("a grant to app_audit precedes the default-deny prologue")

    if problems:
        record(FAIL, "app_audit grant shape", "; ".join(problems[:6]))
    else:
        record(
            PASS,
            "app_audit grant shape",
            f"{len(matrix)} tables, column-scoped UPDATE, no blanket grants",
        )


def check_timestamp_timezone_awareness() -> None:
    """Every TIMESTAMPTZ column in SQL must map to a tz-aware Python type.

    A bare `Mapped[datetime]` defaults to TIMESTAMP WITHOUT TIME ZONE.
    Against a TIMESTAMPTZ column that produces naive datetimes on read and
    raises on comparison with aware ones - a bug that hides until the first
    timezone-sensitive query.

    The fix is `type_annotation_map` on the declarative Base. This asserts it
    is still there, and that no model overrides it with a naive DateTime.
    """
    base = (ROOT / "packages" / "core" / "db" / "base.py").read_text(encoding="utf-8")
    problems: list[str] = []

    if "type_annotation_map" not in base:
        problems.append("Base has no type_annotation_map")
    elif "datetime: DateTime(timezone=True)" not in base:
        problems.append("Base does not map datetime -> DateTime(timezone=True)")

    # No model may hand-roll a naive DateTime.
    for path in sorted(MODELS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"DateTime\(\s*\)", text) or re.search(
            r"DateTime\(timezone\s*=\s*False", text
        ):
            problems.append(f"{path.name} declares a naive DateTime")

    # Cross-check: count TIMESTAMPTZ columns in SQL vs datetime columns in models.
    sql = UP_SQL.read_text(encoding="utf-8")
    tz_columns = len(re.findall(r"\bTIMESTAMPTZ\b", sql))
    if tz_columns == 0:
        problems.append("no TIMESTAMPTZ columns found in the migration")

    if problems:
        record(FAIL, "timestamps are timezone-aware", "; ".join(problems))
    else:
        record(
            PASS,
            "timestamps are timezone-aware",
            f"{tz_columns} TIMESTAMPTZ columns, mapped at Base",
        )


def check_down_migration() -> None:
    up = UP_SQL.read_text(encoding="utf-8")
    down = DOWN_SQL.read_text(encoding="utf-8")
    problems: list[str] = []

    for table in re.findall(r"^CREATE TABLE (\w+)", up, re.MULTILINE):
        if f"DROP TABLE IF EXISTS {table} CASCADE;" not in down:
            problems.append(f"table {table} not dropped")
    for enum in re.findall(r"^CREATE TYPE (\w+) AS ENUM", up, re.MULTILINE):
        if f"DROP TYPE IF EXISTS {enum};" not in down:
            problems.append(f"type {enum} not dropped")
    for func in set(re.findall(r"CREATE OR REPLACE FUNCTION (\w+)", up)):
        if f"DROP FUNCTION IF EXISTS {func}" not in down:
            problems.append(f"function {func} not dropped")

    if problems:
        record(FAIL, "down migration reverses up", "; ".join(problems[:5]))
    else:
        record(PASS, "down migration reverses up", "all tables, types, functions")


def check_migration_chain() -> None:
    versions = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    #: revision id -> the revision it follows, or None for the base.
    revisions: dict[str, str | None] = {}
    for path in versions:
        source = path.read_text(encoding="utf-8")
        revision_match = re.search(r'^revision: str = "([^"]+)"', source, re.MULTILINE)
        down_match = re.search(
            r'^down_revision: str \| None = (?:"([^"]+)"|None)', source, re.MULTILINE
        )
        if revision_match:
            parent = down_match.group(1) if down_match else None
            revisions[revision_match.group(1)] = parent or None

    bases = [r for r, d in revisions.items() if d is None]
    heads = [r for r in revisions if r not in set(revisions.values())]
    problems: list[str] = []
    if len(bases) != 1:
        problems.append(f"{len(bases)} base revisions, expected 1")
    if len(heads) != 1:
        problems.append(f"{len(heads)} heads, expected 1")
    # Distinct names from the `re.Match` objects above. Reusing `rev`/`down`
    # worked at runtime - the for loop rebinds them - but it gave one pair of
    # names two unrelated types in one function, so a reader (and a type
    # checker) had to track which half of the function they were in to know
    # whether `down` was a Match or a string.
    for revision_id, parent_id in revisions.items():
        if parent_id is not None and parent_id not in revisions:
            problems.append(f"{revision_id} points at missing revision {parent_id}")

    if problems:
        record(FAIL, "migration chain is linear", "; ".join(problems))
    else:
        record(PASS, "migration chain is linear", f"{len(revisions)} revisions, head={heads[0]}")


def main() -> int:
    models = model_tables()
    sql = sql_tables()

    check_tables(models, sql)
    check_columns(models, sql)
    check_enums()
    check_fk_order()
    check_design_constraints()
    check_security_invariants()
    check_audit_role_containment()
    check_timestamp_timezone_awareness()
    check_down_migration()
    check_migration_chain()

    width = max(len(name) for _, name, _ in results) + 2
    print("\nPhase 2 schema conformance (models <-> migration <-> design)")
    print("=" * (width + 46))
    for status, name, detail in results:
        marker = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
        print(f"[{marker}] {name:<{width}} {detail}")
    print("=" * (width + 46))
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"{len(results) - failed} passed, {failed} failed")
    print(f"tables: {len(sql)}   enums: {len(sql_enums())}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
