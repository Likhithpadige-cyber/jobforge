"""Timezone-awareness regression tests.

Background: every timestamp column in the migration is TIMESTAMPTZ, but a
bare `Mapped[datetime]` maps to SQLAlchemy's default `DateTime()`, which is
TIMESTAMP WITHOUT TIME ZONE. The two disagreed silently across all 55 model
columns. Symptoms would have been naive datetimes on read, TypeError on any
aware/naive comparison, and Alembic proposing a spurious type change forever.

The fix is `type_annotation_map` on the declarative Base. These tests fail if
that is removed, if a model overrides it with a naive type, or if naive
datetime construction creeps back into application code.

Two layers on purpose:

* `TestModelColumnTypes` reflects the real SQLAlchemy metadata. This is the
  authoritative check and needs SQLAlchemy installed.
* `TestStaticGuards` reads source text and runs with no dependencies at all,
  so drift is caught in CI even before `pip install`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "packages" / "core" / "models"

pytestmark = pytest.mark.unit


# ===========================================================================
# Authoritative: reflect the actual mapped column types.
# ===========================================================================
class TestModelColumnTypes:
    """Requires SQLAlchemy. This is the check that actually proves the fix."""

    @staticmethod
    def _timestamp_columns() -> list[tuple[str, str, object]]:
        from sqlalchemy import DateTime

        import packages.core.models  # noqa: F401  - populates Base.metadata
        from packages.core.db.base import Base

        found = []
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    found.append((table.name, column.name, column.type))
        return found

    def test_every_datetime_column_is_timezone_aware(self) -> None:
        columns = self._timestamp_columns()
        naive = [
            f"{table}.{column}"
            for table, column, column_type in columns
            if not getattr(column_type, "timezone", False)
        ]
        assert not naive, (
            f"{len(naive)} datetime column(s) are timezone-NAIVE while the "
            f"database stores TIMESTAMPTZ: {naive}. "
            "Fix at packages/core/db/base.py via type_annotation_map - "
            "do not patch columns individually."
        )

    def test_expected_number_of_timestamp_columns(self) -> None:
        """Guards against the map silently applying to nothing.

        If a refactor left the map in place but detached from the models,
        the previous test would pass vacuously on an empty list.
        """
        columns = self._timestamp_columns()
        assert len(columns) >= 55, (
            f"only {len(columns)} datetime columns found; expected at least 55. "
            "Either models are not imported, or the annotation map is not applied."
        )

    def test_type_annotation_map_is_configured(self) -> None:
        from sqlalchemy import DateTime

        from packages.core.db.base import Base

        mapping = getattr(Base, "type_annotation_map", {})
        datetime_type = next(
            (v for k, v in mapping.items() if getattr(k, "__name__", "") == "datetime"),
            None,
        )
        assert datetime_type is not None, "Base.type_annotation_map has no datetime entry"
        assert isinstance(datetime_type, DateTime)
        assert datetime_type.timezone is True

    def test_server_defaults_remain_compatible(self) -> None:
        """PostgreSQL `now()` returns timestamptz, so it is correct for these
        columns. A default of `LOCALTIMESTAMP` or `now()::timestamp` would not
        be, and would reintroduce the mismatch through the back door."""
        import packages.core.models  # noqa: F401
        from sqlalchemy import DateTime
        from packages.core.db.base import Base

        bad: list[str] = []
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if not isinstance(column.type, DateTime):
                    continue
                default = column.server_default
                if default is None:
                    continue
                text = str(getattr(default, "arg", default)).lower()
                if "localtimestamp" in text or "::timestamp" in text:
                    bad.append(f"{table.name}.{column.name} -> {text}")
        assert not bad, f"timezone-naive server defaults: {bad}"


# ===========================================================================
# Dependency-free: text-level guards that run anywhere.
# ===========================================================================
class TestStaticGuards:
    def test_base_declares_the_annotation_map(self) -> None:
        source = (ROOT / "packages" / "core" / "db" / "base.py").read_text()
        assert "type_annotation_map" in source
        assert "datetime: DateTime(timezone=True)" in source

    def test_no_model_declares_a_naive_datetime(self) -> None:
        """A one-off `DateTime()` or `DateTime(timezone=False)` in a model
        would override the Base map for that column only - exactly the drift
        this whole exercise removed."""
        offenders: list[str] = []
        for path in sorted(MODELS_DIR.glob("*.py")):
            text = path.read_text()
            if re.search(r"DateTime\(\s*\)", text):
                offenders.append(f"{path.name}: bare DateTime()")
            if re.search(r"DateTime\(\s*timezone\s*=\s*False", text):
                offenders.append(f"{path.name}: DateTime(timezone=False)")
        assert not offenders, offenders

    def test_no_redundant_per_column_datetime_overrides(self) -> None:
        """The Base map handles every column, so an explicit
        `DateTime(timezone=True)` in a model is a leftover one-off fix.

        Harmless at runtime, but it hides whether the root fix is actually
        doing the work - so it should be removed.
        """
        offenders: list[str] = []
        for path in sorted(MODELS_DIR.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "DateTime(timezone=True)" in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, (
            "redundant per-column DateTime overrides; the Base "
            f"type_annotation_map already covers these: {offenders}"
        )

    def test_no_utcnow_anywhere(self) -> None:
        """`datetime.utcnow()` returns a NAIVE datetime despite the name.
        It is the most common source of this bug class in Python.

        Parsed as calls rather than grepped as text, so documentation that
        warns against it does not trip its own check.
        """
        offenders: list[str] = []
        for path in ROOT.rglob("*.py"):
            if any(part in {"node_modules", "__pycache__", ".venv"} for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "utcnow"
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert not offenders, f"datetime.utcnow() called at: {offenders}"

    def test_application_code_uses_the_canonical_clock(self) -> None:
        """Outside clock.py and tests, timestamps come from utc_now()."""
        allowed = {
            Path("packages/shared/clock.py"),
        }
        offenders: list[str] = []
        for directory in ("packages", "apps"):
            for path in (ROOT / directory).rglob("*.py"):
                relative = path.relative_to(ROOT)
                if relative in allowed or "__pycache__" in str(relative):
                    continue
                source = path.read_text()
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "now"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "datetime"
                    ):
                        offenders.append(f"{relative}:{node.lineno}")
        assert not offenders, (
            "use utc_now() from packages.shared.clock instead of "
            f"datetime.now(...): {offenders}"
        )

    def test_ruff_enforces_datetimez(self) -> None:
        """The lint rule is the guard that catches this in review."""
        config = (ROOT / "pyproject.toml").read_text()
        assert '"DTZ"' in config, "enable the DTZ (flake8-datetimez) ruff rule"

    def test_migration_uses_timestamptz_not_timestamp(self) -> None:
        """Item 3: the database schema is correct and must not be changed to
        accommodate the ORM. This asserts nobody 'fixed' it the wrong way."""
        sql = (ROOT / "migrations" / "sql" / "0002_core_schema.up.sql").read_text()
        naive = re.findall(r"\bTIMESTAMP\b(?!TZ)", sql)
        assert not naive, (
            f"{len(naive)} naive TIMESTAMP column(s) in the migration; "
            "the schema should stay TIMESTAMPTZ and the ORM should match it"
        )
        assert sql.count("TIMESTAMPTZ") >= 55


class TestClockHelpers:
    def test_utc_now_is_aware(self) -> None:
        from packages.shared.clock import is_aware, utc_now

        assert is_aware(utc_now())

    def test_ensure_utc_attaches_utc_to_naive(self) -> None:
        from datetime import datetime

        from packages.shared.clock import ensure_utc, is_aware

        naive = datetime(2026, 9, 9, 12, 0)  # noqa: DTZ001
        result = ensure_utc(naive)
        assert result is not None
        assert is_aware(result)
        assert result.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_ensure_utc_converts_other_zones(self) -> None:
        from datetime import datetime, timedelta, timezone

        from packages.shared.clock import ensure_utc

        eastern = timezone(timedelta(hours=-5))
        value = datetime(2026, 9, 9, 12, 0, tzinfo=eastern)
        result = ensure_utc(value)
        assert result is not None
        assert result.hour == 17

    def test_ensure_utc_passes_none_through(self) -> None:
        from packages.shared.clock import ensure_utc

        assert ensure_utc(None) is None
