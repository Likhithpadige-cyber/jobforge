"""The effective privilege boundary of app_audit, proved against PostgreSQL.

C3_HARDENING_DESIGN.md I.6.6 and J.25(g)-(i), plus J.19's half of the
attempts-table grants. `verify_schema.py` proves the migration DIFF cannot
widen the role; this file proves what the CLUSTER actually resolves, which is
a different claim: effective privilege is the closure over PUBLIC, role
memberships, default privileges and function ownership, and no amount of text
parsing establishes it. Neither half is sufficient alone.

`information_schema.role_table_grants` is deliberately not used. It reports
grants rather than effective access, does not decompose column privileges, and
silently attributes a privilege held via PUBLIC to whichever role you filter
on. `has_table_privilege` and friends answer the question actually being
asked.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

pytestmark = [pytest.mark.integration, pytest.mark.safety]

TABLE_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
)

#: I.6.1, exactly. Table -> row privileges that must be true.
AUDIT_MATRIX: dict[str, set[str]] = {
    "agent_runs": {"SELECT", "INSERT"},
    "agent_run_steps": {"SELECT", "INSERT"},
    "llm_calls": {"SELECT", "INSERT"},
    "events": {"SELECT", "INSERT"},
}

#: The only columns app_audit may UPDATE, per table.
AUDIT_UPDATE_COLUMNS: dict[str, set[str]] = {
    "agent_runs": {
        "status", "steps_completed", "input_tokens", "output_tokens", "cost_usd",
        "prompt_version", "error_type", "error_message", "duration_ms",
        "finished_at",
    },
    "agent_run_steps": {
        "status", "input_digest", "output", "guardrail_results", "duration_ms",
        "finished_at",
    },
    "llm_calls": {"response_redacted"},
    "events": set(),
}

DOMAIN_TABLES = (
    "applications", "resumes", "resume_claims", "approvals", "profile_facts",
    "profiles", "resume_revision_attempts", "settings", "users",
)


# =====================================================================
# (A) exact table privileges, over EVERY table - not just the four
# =====================================================================
async def test_effective_table_privileges_match_the_matrix(audit_engine) -> None:
    """Every allowed cell true, every other cell false, across all of public.

    Scoped to every table rather than the four, because the failure this
    catches is a table added by a later migration arriving with a grant nobody
    intended - which by definition is not in the four.
    """
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname, p.privilege, "
                "       has_table_privilege('app_audit', c.oid, p.privilege) "
                "  FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                " CROSS JOIN unnest(CAST(:privs AS text[])) AS p(privilege) "
                " WHERE n.nspname = 'public' AND c.relkind = 'r'"
            ),
            {"privs": list(TABLE_PRIVILEGES)},
        )
        unexpected = [
            f"{table}.{privilege}={held}"
            for table, privilege, held in rows
            if held is not (privilege in AUDIT_MATRIX.get(table, set()))
        ]
    assert not unexpected, f"effective table privileges diverge from I.6.1: {unexpected}"


# =====================================================================
# (B) exact column UPDATE privileges
# =====================================================================
async def test_effective_column_update_privileges_match_the_matrix(
    audit_engine,
) -> None:
    """The check that pins llm_calls.

    `has_column_privilege` returns true for every column when the TABLE-level
    privilege is held, so on its own it cannot tell a column-scoped grant from
    a table-wide one. It is asserted together with the table check above,
    never separately: that pair is what distinguishes
    `GRANT UPDATE (response_redacted)` from `GRANT UPDATE`.
    """
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname, a.attname, "
                "       has_column_privilege('app_audit', c.oid, a.attname, 'UPDATE') "
                "  FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                "  JOIN pg_attribute a ON a.attrelid = c.oid "
                " WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "   AND a.attnum > 0 AND NOT a.attisdropped "
                "   AND c.relname = ANY(CAST(:tables AS text[]))"
            ),
            {"tables": list(AUDIT_UPDATE_COLUMNS)},
        )
        unexpected = [
            f"{table}.{column}={held}"
            for table, column, held in rows
            if held is not (column in AUDIT_UPDATE_COLUMNS[table])
        ]
    assert not unexpected, f"column UPDATE privileges diverge from I.6.1: {unexpected}"


# =====================================================================
# (C) role properties
# =====================================================================
async def test_audit_role_holds_no_dangerous_attribute(audit_engine) -> None:
    """NOINHERIT is the one people forget: it is what stops a membership
    added by mistake from conferring privileges implicitly."""
    async with audit_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                    "       rolreplication, rolinherit "
                    "  FROM pg_roles WHERE rolname = 'app_audit'"
                )
            )
        ).one()
    assert row == (False, False, False, False, False, False), (
        "app_audit must be NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, "
        f"NOREPLICATION and NOINHERIT; got {row}"
    )


# =====================================================================
# (D) PUBLIC and membership leakage
# =====================================================================
async def test_audit_role_is_in_no_role_and_holds_no_members(audit_engine) -> None:
    async with audit_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_auth_members "
                    " WHERE member = 'app_audit'::regrole "
                    "    OR roleid = 'app_audit'::regrole"
                )
            )
        ).scalar_one()
    assert count == 0, "app_audit participates in a role membership"


async def test_public_holds_no_table_privilege(audit_engine) -> None:
    """Without this, a privilege reaching app_audit through PUBLIC would
    satisfy the matrix check while being available to every role."""
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname, p.privilege "
                "  FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                " CROSS JOIN unnest(CAST(:privs AS text[])) AS p(privilege) "
                " WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "   AND has_table_privilege('public', c.oid, p.privilege)"
            ),
            {"privs": list(TABLE_PRIVILEGES)},
        )
        leaked = [f"{table}.{privilege}" for table, privilege in rows]
    assert not leaked, f"PUBLIC holds table privileges: {leaked}"


async def test_audit_privileges_are_held_directly_and_are_not_grantable(
    audit_engine,
) -> None:
    """`aclexplode` shows HOW the privilege is held, which the matrix cannot."""
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname, acl.privilege_type, acl.is_grantable "
                "  FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                " CROSS JOIN aclexplode(c.relacl) AS acl "
                " WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "   AND acl.grantee = 'app_audit'::regrole"
            )
        )
        problems = [
            f"{table}.{privilege} grantable={grantable}"
            for table, privilege, grantable in rows
            if grantable or privilege not in AUDIT_MATRIX.get(table, set()) | {"UPDATE"}
        ]
    assert not problems, f"unexpected app_audit ACL entries: {problems}"


# =====================================================================
# (E) executable functions
# =====================================================================
async def test_audit_role_cannot_execute_the_fact_usage_helper(audit_engine) -> None:
    """The single most important line of (E).

    `jobforge_increment_fact_usage` is SECURITY DEFINER and its body does
    `UPDATE profile_facts SET usage_count = usage_count + 1`. It is the one
    directly-callable domain-mutating function in the schema, so EXECUTE on it
    would hand app_audit a domain write through the front door.
    """
    async with audit_engine.connect() as conn:
        held = (
            await conn.execute(
                text(
                    "SELECT has_function_privilege('app_audit', p.oid, 'EXECUTE') "
                    "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    " WHERE n.nspname = 'public' "
                    "   AND p.proname = 'jobforge_increment_fact_usage'"
                )
            )
        ).scalar_one()
    assert held is False


async def test_every_executable_security_definer_function_is_a_trigger(
    audit_engine,
) -> None:
    """A bounded, named exception rather than a blanket allowance.

    plpgsql functions are EXECUTE-able by PUBLIC by default and only
    `jobforge_increment_fact_usage` is revoked, so app_audit does hold EXECUTE
    on the trigger functions. That is accepted because PostgreSQL refuses to
    invoke a `RETURNS trigger` function outside trigger context - the
    privilege exists but is not exercisable, which (F) demonstrates rather
    than argues. Expressed as a query, not a list, so a future SECURITY
    DEFINER function that is NOT a trigger function fails this the day it is
    added rather than the day it is abused.
    """
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT p.proname "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'public' AND p.prosecdef "
                "   AND p.prorettype <> 'trigger'::regtype "
                "   AND has_function_privilege('app_audit', p.oid, 'EXECUTE')"
            )
        )
        callable_definers = [name for (name,) in rows]
    assert not callable_definers, (
        "app_audit can EXECUTE non-trigger SECURITY DEFINER functions: "
        f"{callable_definers}"
    )


# =====================================================================
# (F) direct SQL, as app_audit
# =====================================================================
@pytest.mark.parametrize("table", DOMAIN_TABLES)
@pytest.mark.parametrize("verb", ["INSERT", "UPDATE", "DELETE"])
async def test_audit_role_cannot_write_domain_tables(
    audit_engine, table, verb
) -> None:
    """Catalog assertions can still be wrong about the world; try it."""
    statements = {
        "INSERT": f"INSERT INTO {table} DEFAULT VALUES",
        "UPDATE": f"UPDATE {table} SET id = id",  # noqa: S608
        "DELETE": f"DELETE FROM {table}",  # noqa: S608
    }
    if table == "settings" and verb == "UPDATE":
        statements["UPDATE"] = "UPDATE settings SET key = key"
    async with audit_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text(statements[verb]))
    assert "permission denied" in str(exc.value).lower()


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM events",
        "DELETE FROM agent_runs",
        "DELETE FROM agent_run_steps",
        "DELETE FROM llm_calls",
        "TRUNCATE agent_runs",
        "UPDATE llm_calls SET prompt_redacted = ''",
        "UPDATE llm_calls SET cost_usd = 0",
        "UPDATE events SET published = true",
        "UPDATE agent_runs SET started_at = now()",
        "UPDATE agent_run_steps SET step_name = 'x'",
        "CREATE TABLE public.t_leak (i int)",
    ],
)
async def test_audit_role_is_refused_outside_its_matrix(
    audit_engine, statement
) -> None:
    """Each case is a column or verb the matrix withholds.

    `UPDATE events SET published` fails on privilege, before the
    events-immutability trigger is ever consulted - the role is the first
    line, not the trigger.
    """
    async with audit_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text(statement))
    assert "permission denied" in str(exc.value).lower()


async def test_a_cte_wrapped_domain_write_is_still_refused(audit_engine) -> None:
    """The sub-case that makes I.6.4's claim falsifiable.

    A CTE-wrapped write is exactly what the statement-parsing guard is
    documented NOT to catch. The role catches it anyway, which is why the
    guard is a diagnostic and the role is the enforcement.
    """
    async with audit_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text(
                    "WITH x AS ("
                    "  UPDATE applications SET status = 'resume_ready' RETURNING 1"
                    ") SELECT * FROM x"
                )
            )
    assert "permission denied" in str(exc.value).lower()


async def test_audit_role_has_no_privilege_on_revision_attempts(
    audit_engine,
) -> None:
    """resume_revision_attempts is not an audit table (H.4)."""
    async with audit_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT p.privilege "
                " FROM unnest(CAST(:privs AS text[])) AS p(privilege) "
                " WHERE has_table_privilege("
                "   'app_audit', 'resume_revision_attempts', p.privilege)"
            ),
            {"privs": list(TABLE_PRIVILEGES)},
        )
        held = [privilege for (privilege,) in rows]
    assert not held, f"app_audit holds {held} on resume_revision_attempts"


# =====================================================================
# J.19: the attempts table's own grants
# =====================================================================
async def test_agent_cannot_insert_a_revision_attempt(agent_engine) -> None:
    """An attempt is created by a human request, never by a worker - the same
    principle that keeps app_agent off `approvals`."""
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("INSERT INTO resume_revision_attempts DEFAULT VALUES")
            )
    assert "permission denied" in str(exc.value).lower()


@pytest.mark.parametrize(
    "column", ["abandoned_by", "abandon_reason", "replaces_attempt_id", "recovered_at"]
)
async def test_agent_cannot_write_the_human_only_columns(
    agent_engine, column
) -> None:
    """Column-level UPDATE is what makes "a worker cannot release its own
    attempt" physical rather than procedural."""
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            sql = f"UPDATE resume_revision_attempts SET {column} = NULL"  # noqa: S608
            await conn.execute(text(sql))
    assert "permission denied" in str(exc.value).lower()


async def test_nobody_may_delete_a_revision_attempt(agent_engine) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text("DELETE FROM resume_revision_attempts"))
    assert "permission denied" in str(exc.value).lower()
