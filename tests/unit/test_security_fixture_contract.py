"""Static guards on the security-test connection path.

None of these needs a database. They protect the properties that decide
whether the 17 agent-isolation tests actually execute on a real machine:

* the diagnostic must build its DSN exactly as the fixture does, or a green
  `make diagnose-roles` can coexist with a skipping test suite;
* REQUIRE_SECURITY_TESTS=1 must be fail-closed on every path out of the
  fixtures, not just the connection failure;
* the fixture must never fall back to a privileged role.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "integration" / "conftest.py"
DIAGNOSE = ROOT / "scripts" / "diagnose_roles.py"
SECURITY_TESTS = ROOT / "tests" / "integration" / "test_security_invariants.py"

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def _function_node(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _function_body(path: Path, name: str) -> str:
    return ast.unparse(_function_node(path, name))


def _executable_statements(path: Path, name: str) -> list[str]:
    """The function's executable statements, docstring excluded.

    Comparing raw source would match prose: both functions explain in their
    docstrings why they use rpartition, and that commentary is not code.
    """
    node = _function_node(path, name)
    statements = list(node.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]

    normalised: list[str] = []
    for statement in statements:
        # The fixture has an extra early-return for the owner DSN; the
        # diagnostic is only ever called with a role. Ignore that branch.
        if isinstance(statement, ast.If):
            continue
        rendered = re.sub(r"\s+", " ", ast.unparse(statement)).strip()
        # settings lookup differs cosmetically (local variable vs inline call)
        rendered = rendered.replace("settings = get_settings()", "")
        rendered = rendered.replace(
            "str(settings.database_url)", "str(get_settings().database_url)"
        )
        if rendered:
            normalised.append(rendered)
    return normalised


class TestDiagnosticMirrorsFixture:
    def test_dsn_construction_is_identical(self) -> None:
        """If these drift, `make diagnose-roles` can report a healthy
        connection while the fixture still cannot connect - the worst
        possible diagnostic outcome."""
        fixture = _executable_statements(CONFTEST, "_url")
        diagnostic = _executable_statements(DIAGNOSE, "agent_url")
        assert fixture == diagnostic, (
            "conftest._url and diagnose_roles.agent_url build the DSN "
            f"differently.\nfixture:    {fixture}\ndiagnostic: {diagnostic}"
        )

    def test_both_use_rpartition_for_the_host(self) -> None:
        """partition() would split at an '@' inside the owner password."""
        for path, name in ((CONFTEST, "_url"), (DIAGNOSE, "agent_url")):
            body = _function_body(path, name)
            assert "rpartition" in body, f"{path.name}::{name} must use rpartition"

    def test_both_percent_encode_the_password(self) -> None:
        for path, name in ((CONFTEST, "_url"), (DIAGNOSE, "agent_url")):
            assert "quote(" in _function_body(path, name)


class TestFailClosed:
    def test_every_skip_path_goes_through_the_helper(self) -> None:
        """A bare pytest.skip() in the integration conftest would bypass
        REQUIRE_SECURITY_TESTS and let a green summary mean nothing."""
        source = CONFTEST.read_text()
        bare_skips = [
            f"line {number}: {line.strip()}"
            for number, line in enumerate(source.splitlines(), 1)
            if "pytest.skip(" in line and "def _skip_or_fail" not in line
        ]
        # Only the helper itself may call pytest.skip directly.
        helper = _function_body(CONFTEST, "_skip_or_fail")
        allowed = sum(1 for line in helper.splitlines() if "pytest.skip(" in line)
        assert (
            len(bare_skips) == allowed
        ), f"pytest.skip() called outside _skip_or_fail: {bare_skips}"

    def test_helper_fails_when_security_is_required(self) -> None:
        helper = _function_body(CONFTEST, "_skip_or_fail")
        assert "REQUIRE_SECURITY_TESTS" in helper
        assert "pytest.fail(" in helper

    def test_make_target_sets_the_flag(self) -> None:
        makefile = (ROOT / "Makefile").read_text()
        target = makefile[makefile.index("test-security:") :]
        target = target[: target.index("\n\n")]
        assert (
            "REQUIRE_SECURITY_TESTS=1" in target
        ), "make test-security must set REQUIRE_SECURITY_TESTS=1 so skips fail"


class TestNoPrivilegedFallback:
    def test_fixture_never_falls_back_to_the_owner_role(self) -> None:
        body = _function_body(CONFTEST, "agent_engine")
        assert '_url("app_agent")' in body or "_url('app_agent')" in body
        # A fallback would look like a second _url() call with no role.
        assert body.count("_url(") == 1, (
            "agent_engine builds more than one DSN; a privileged fallback "
            "would make every isolation test meaningless"
        )

    def test_no_superuser_credentials_in_the_security_tests(self) -> None:
        source = SECURITY_TESTS.read_text()
        for forbidden in ("postgres_password", "jobforge:", "POSTGRES_USER", "superuser"):
            assert forbidden not in source, (
                f"{forbidden!r} appears in the security tests; they must use "
                "the app_agent fixture only"
            )

    def test_all_isolation_tests_use_the_agent_engine(self) -> None:
        tree = ast.parse(SECURITY_TESTS.read_text())
        tests = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_")
        ]
        assert tests, "no tests found"
        missing = [
            node.name for node in tests if "agent_engine" not in {arg.arg for arg in node.args.args}
        ]
        assert not missing, f"tests not using agent_engine: {missing}"


class TestDiagnosticCoverage:
    @pytest.mark.parametrize(
        "probe",
        [
            "pg_roles",  # role existence
            "rolcanlogin",  # LOGIN capability
            "rolpassword",  # password configured
            "has_database_privilege",  # CONNECT
            "has_schema_privilege",  # USAGE / CREATE on public
            "has_table_privilege",  # SELECT / INSERT / UPDATE / DELETE
            "has_function_privilege",  # the one permitted write path
            "current_user",  # actual connection result
        ],
    )
    def test_diagnostic_probes_are_present(self, probe: str) -> None:
        assert probe in DIAGNOSE.read_text(), f"diagnose_roles.py does not check {probe}"

    def test_diagnostic_reports_the_exact_exception(self) -> None:
        source = DIAGNOSE.read_text()
        assert "type(exc).__name__" in source
        assert "{exc}" in source
