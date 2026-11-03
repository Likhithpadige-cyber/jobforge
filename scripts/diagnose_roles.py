#!/usr/bin/env python3
"""Diagnose why the app_agent security tests are not executing.

The 16 agent-isolation tests connect AS the app_agent role and assert its
writes to profile_facts are refused. When that connection cannot be made they
skip, which reads as a pass in the pytest summary while proving nothing.

This script answers, in order:

  1. Does the role exist?
  2. Can it LOGIN?  (and is it wrongly a superuser?)
  3. Does it have a password set?
  4. Does the pytest fixture's exact URL connect - and if not, what is the
     precise exception?
  5-8. SELECT / INSERT / UPDATE / DELETE privilege on profile_facts.
  9. Is the one permitted write path (the SECURITY DEFINER usage function)
     reachable?

Every check is individually guarded. An earlier version aborted at step 5 when
the role was missing, which suppressed the connection exception - the single
most useful line of output. A diagnostic that stops at the first problem is
not much of a diagnostic.

    make diagnose-roles
    docker compose exec api python scripts/diagnose_roles.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.config import get_settings

ROLES = ("app_api", "app_agent", "app_monitor", "app_migrate")
TARGET_ROLE = "app_agent"
TARGET_TABLE = "profile_facts"

#: What app_agent must be able to do, and must not.
EXPECTED_PRIVILEGES = {
    "SELECT": True,
    "INSERT": False,
    "UPDATE": False,
    "DELETE": False,
}

OK = "  ok  "
BAD = " FAIL "
INFO = " info "

problems: list[str] = []
notes: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label:<46} {detail}")


def agent_url(role: str) -> str:
    """Build the URL EXACTLY as tests/integration/conftest.py::_url does.

    Kept identical in behaviour on purpose: if this connects and the fixture
    does not, the difference is in the fixture, not the database. rpartition
    (not partition) so an '@' inside the owner password does not break host
    detection; quote() so special characters in the role password survive.
    """
    url = str(get_settings().database_url)
    password = os.getenv(f"{role.upper()}_PASSWORD", role)
    prefix, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{prefix}://{quote(role, safe='')}:{quote(password, safe='')}@{hostpart}"


def redact(url: str) -> str:
    prefix, _, rest = url.partition("://")
    user, _, hostpart = rest.partition("@")
    return f"{prefix}://{user.split(':')[0]}:***@{hostpart}"


async def _check_role_identity(conn) -> tuple[bool, bool]:  # type: ignore[no-untyped-def]
    """Steps 1-2: existence, LOGIN, and the superuser trap."""
    try:
        row = (
            await conn.execute(
                text("SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = :name"),
                {"name": TARGET_ROLE},
            )
        ).first()
    except Exception as exc:
        line(BAD, "1-2. role existence / LOGIN", f"{type(exc).__name__}: {exc}")
        problems.append(f"could not read pg_roles: {exc}")
        return False, False

    if row is None:
        line(BAD, "1. role exists", f"{TARGET_ROLE} NOT FOUND")
        problems.append(
            f"{TARGET_ROLE} does not exist - migration 0003 never ran. Check that "
            "JOBFORGE_SKIP_ROLE_GRANTS is not set to 1, then run: make migrate"
        )
        return False, False

    line(OK, "1. role exists", TARGET_ROLE)
    can_login = bool(row[0])
    if can_login:
        line(OK, "2. LOGIN capability", "rolcanlogin = true")
    else:
        line(BAD, "2. LOGIN capability", "rolcanlogin = FALSE")
        problems.append(f"{TARGET_ROLE} cannot log in. Fix: ALTER ROLE {TARGET_ROLE} LOGIN;")

    if row[1]:
        line(BAD, "   role is NOT a superuser", "rolsuper = TRUE")
        problems.append(
            f"{TARGET_ROLE} is a SUPERUSER - it bypasses every grant, so the "
            "isolation tests would pass while proving nothing"
        )
    return True, can_login


async def _check_password(conn) -> None:  # type: ignore[no-untyped-def]
    """Step 3. Needs superuser to read pg_authid; degrades to a note."""
    try:
        has_password = (
            await conn.execute(
                text("SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = :n"),
                {"n": TARGET_ROLE},
            )
        ).scalar_one_or_none()
    except Exception:
        line(INFO, "3. password set", "pg_authid unreadable (needs superuser)")
        notes.append("password presence could not be read directly")
        return

    if has_password is None:
        line(INFO, "3. password set", "role not present in pg_authid")
    elif has_password:
        line(OK, "3. password set", "yes")
    else:
        line(BAD, "3. password set", "NO PASSWORD")
        problems.append(
            f"{TARGET_ROLE} has no password. PostgreSQL authenticates host "
            "connections with scram-sha-256, and a role without a password can "
            "never authenticate no matter how correct its grants are. "
            "Fix: make db-roles"
        )


async def _check_connect_and_schema(conn) -> None:  # type: ignore[no-untyped-def]
    """Two failure modes that look like something else if you do not check them.

    CONNECT on the database: if this were revoked from PUBLIC, app_agent
    would fail authentication with a message that reads like a bad password.

    USAGE on schema public: subtler and more dangerous. Without it the role
    connects fine, but EVERY statement fails with "permission denied for
    schema public". The 15 negative tests assert exactly that string, so they
    would all PASS - for entirely the wrong reason, while the table-level
    grants could be wide open underneath.
    """
    try:
        can_connect = (
            await conn.execute(
                text("SELECT has_database_privilege(:role, current_database(), 'CONNECT')"),
                {"role": TARGET_ROLE},
            )
        ).scalar_one()
    except Exception as exc:
        line(INFO, "3a. CONNECT on database", f"{type(exc).__name__}: {exc}")
        return
    line(OK if can_connect else BAD, "3a. CONNECT on database", str(can_connect))
    if not can_connect:
        problems.append(
            f"{TARGET_ROLE} lacks CONNECT on the database; authentication will "
            "fail in a way that looks like a wrong password"
        )

    try:
        schema_usage = (
            await conn.execute(
                text("SELECT has_schema_privilege(:role, 'public', 'USAGE')"),
                {"role": TARGET_ROLE},
            )
        ).scalar_one()
    except Exception as exc:
        line(INFO, "3b. USAGE on schema public", f"{type(exc).__name__}: {exc}")
        return
    line(OK if schema_usage else BAD, "3b. USAGE on schema public", str(schema_usage))
    if not schema_usage:
        problems.append(
            f"{TARGET_ROLE} lacks USAGE on schema public. Every statement would "
            "fail with 'permission denied for schema public', which the negative "
            "tests would misread as correct table-level isolation."
        )

    # The role must NOT be able to create objects - that is the workaround path.
    try:
        can_create = (
            await conn.execute(
                text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
                {"role": TARGET_ROLE},
            )
        ).scalar_one()
    except Exception:
        return
    line(
        OK if not can_create else BAD,
        "3c. CREATE on schema public",
        f"{can_create}  (expected False)",
    )
    if can_create:
        problems.append(
            f"{TARGET_ROLE} can CREATE in schema public; it could define a view "
            "or SECURITY DEFINER function to write around its table grants"
        )


async def _check_privileges(conn) -> None:  # type: ignore[no-untyped-def]
    """Steps 5-8: the privileges the whole security model rests on."""
    for index, (privilege, expected) in enumerate(EXPECTED_PRIVILEGES.items(), start=5):
        label = f"{index}. {privilege:<6} on {TARGET_TABLE}"
        try:
            actual = (
                await conn.execute(
                    text("SELECT has_table_privilege(:role, :tbl, :priv)"),
                    {"role": TARGET_ROLE, "tbl": TARGET_TABLE, "priv": privilege},
                )
            ).scalar_one()
        except Exception as exc:
            line(BAD, label, f"{type(exc).__name__}: {exc}")
            continue

        if actual == expected:
            line(OK, label, f"{actual}  (expected {expected})")
        else:
            line(BAD, label, f"{actual}  (expected {expected})")
            problems.append(
                f"{TARGET_ROLE} {privilege} on {TARGET_TABLE} is {actual}, expected "
                f"{expected}. The grants in migration 0003 did not apply correctly."
            )


async def _check_usage_function(conn) -> None:  # type: ignore[no-untyped-def]
    """Step 9: the single permitted write path."""
    try:
        can_execute = (
            await conn.execute(
                text(
                    "SELECT has_function_privilege(:role, "
                    "'jobforge_increment_fact_usage(uuid)', 'EXECUTE')"
                ),
                {"role": TARGET_ROLE},
            )
        ).scalar_one()
    except Exception as exc:
        line(INFO, "9. EXECUTE on usage function", f"{type(exc).__name__}: {exc}")
        return

    line(OK if can_execute else BAD, "9. EXECUTE on usage function", str(can_execute))
    if not can_execute:
        problems.append(
            "app_agent cannot execute jobforge_increment_fact_usage; the "
            "usage-counter test will fail"
        )


async def main() -> int:
    settings = get_settings()

    print("=" * 80)
    print("app_agent security-fixture diagnostics")
    print("=" * 80)
    print()
    print("Environment")
    print(f"  owner DSN                {redact(str(settings.database_url))}")
    print(f"  POSTGRES_HOST            {os.getenv('POSTGRES_HOST', '(unset)')}")
    print(f"  REQUIRE_SECURITY_TESTS   {os.getenv('REQUIRE_SECURITY_TESTS', '(unset)')}")
    for role in ROLES:
        env = f"{role.upper()}_PASSWORD"
        state = "set" if os.getenv(env) else "UNSET -> falls back to the role name"
        print(f"  {env:<24} {state}")
    print()
    print("Checks")

    owner = create_async_engine(str(settings.database_url))
    try:
        async with owner.connect() as conn:
            role_exists, _ = await _check_role_identity(conn)
            if role_exists:
                await _check_password(conn)
                await _check_connect_and_schema(conn)
                await _check_privileges(conn)
                await _check_usage_function(conn)
            else:
                line(INFO, "3-9. remaining checks", "skipped: role does not exist")
    except Exception as exc:
        line(BAD, "owner connection", f"{type(exc).__name__}: {exc}")
        problems.append(f"cannot connect as the owner role either: {exc}")
    finally:
        await owner.dispose()

    # Step 4 runs regardless: its exception is the most informative output
    # this script produces, and an earlier version suppressed it.
    url = agent_url(TARGET_ROLE)
    print()
    print("4. Connecting exactly as tests/integration/conftest.py::agent_engine does")
    print(f"   url: {redact(url)}")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar_one()
            if who == TARGET_ROLE:
                line(OK, "   connection established", f"current_user = {who}")
            else:
                line(BAD, "   connected as the WRONG role", who)
                problems.append(
                    f"connected as {who}, not {TARGET_ROLE} - the isolation tests "
                    "would be exercising the wrong role"
                )
    except Exception as exc:
        line(BAD, "   connection FAILED", type(exc).__name__)
        print()
        print("   Exact exception:")
        for entry in traceback.format_exception_only(type(exc), exc):
            print(f"     {entry.rstrip()}")
        problems.append(f"fixture connection failed: {type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()

    print()
    print("=" * 80)
    if problems:
        print("The agent-isolation tests are NOT executing. Causes found:")
        for problem in problems:
            print(f"  - {problem}")
        print()
        print("Until this is fixed, a green `make test-security` summary means only")
        print("that the unrelated assertions passed. The agent boundary is UNVERIFIED.")
        print()
        print("Most common fix:  make db-roles")
        return 1

    for note in notes:
        print(f"note: {note}")
    print("All checks passed.")
    print(f"{TARGET_ROLE} exists, can log in, can read {TARGET_TABLE}, and cannot")
    print("INSERT, UPDATE or DELETE it. `make test-security` should now run every")
    print("test with zero skips.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
