"""Schema conformance as a test, not just a script.

Deliberately dependency-free: it shells out to the checkers so it runs even
before pip install, and so CI fails on schema drift without needing a
database. The heavy behavioural checks live in tests/integration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / script)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_models_match_migration_and_design() -> None:
    result = _run("verify_schema.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_sqlalchemy_name_shadowing() -> None:
    """A column named `text` shadows sqlalchemy's text() helper for the rest
    of its class body. This is an import-time crash, not a type error."""
    result = _run("check_model_shadowing.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_down_migration_is_not_stale() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "gen_down_migration.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_agent_role_has_no_write_grant_on_facts() -> None:
    """Read the grants SQL directly: the enforcement must be visible in DDL,
    not only asserted in a runtime test that could be skipped."""
    grants = (ROOT / "migrations" / "sql" / "0003_roles_grants.up.sql").read_text()
    executable = "\n".join(
        line for line in grants.splitlines() if not line.strip().startswith("--")
    )
    assert "REVOKE INSERT, UPDATE, DELETE ON" in executable
    revoke_section = executable.split("REVOKE INSERT, UPDATE, DELETE ON")[1]
    revoke_section = revoke_section.split("FROM app_agent;")[0]
    for table in ("profile_facts", "profiles", "site_policies", "settings", "approvals"):
        assert table in revoke_section, f"{table} not revoked from app_agent"


def test_no_grant_option_anywhere() -> None:
    """WITH GRANT OPTION would let a role widen access for another role."""
    grants = (ROOT / "migrations" / "sql" / "0003_roles_grants.up.sql").read_text()
    executable = "\n".join(
        line for line in grants.splitlines() if not line.strip().startswith("--")
    )
    assert "WITH GRANT OPTION" not in executable


def test_migration_sql_files_exist_and_are_non_empty() -> None:
    sql_dir = ROOT / "migrations" / "sql"
    for name in (
        "0002_core_schema.up.sql",
        "0002_core_schema.down.sql",
        "0003_roles_grants.up.sql",
        "0003_roles_grants.down.sql",
    ):
        path = sql_dir / name
        assert path.is_file(), f"{name} missing"
        assert len(path.read_text().strip()) > 100, f"{name} looks empty"
