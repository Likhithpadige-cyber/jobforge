"""M8 privilege changes and rollback safety, checked by real PostgreSQL."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.shared.sql_script import split_sql_statements

pytestmark = [pytest.mark.integration, pytest.mark.safety]
SQL = Path(__file__).resolve().parents[2] / "migrations" / "sql"


async def run_migration(db, direction):
    for statement in split_sql_statements(
        (SQL / f"0008_artifact_binding.{direction}.sql").read_text()
    ):
        await db.execute(text(statement))


async def privileges(db, table="artifacts"):
    return (
        await db.execute(
            text(
                "SELECT has_table_privilege('app_agent', :table, 'SELECT'), "
                "has_table_privilege('app_agent', :table, 'INSERT'), "
                "has_table_privilege('app_agent', :table, 'UPDATE'), "
                "has_table_privilege('app_agent', :table, 'DELETE'), "
                "has_table_privilege('app_api', :table, 'SELECT'), "
                "has_table_privilege('app_api', :table, 'INSERT'), "
                "has_table_privilege('app_api', :table, 'UPDATE'), "
                "has_table_privilege('app_api', :table, 'DELETE')"
            ),
            {"table": table},
        )
    ).one()


@pytest.fixture
async def migration_table(session):
    # As in the M5 migration tests, isolate DDL in a rollback-only schema.
    # No live tables, Alembic revision rows or cluster roles are rewritten.
    schema = f"m8_{uuid4().hex}"
    await session.execute(text(f'CREATE SCHEMA "{schema}"'))
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await session.execute(text("CREATE TABLE approvals (id uuid PRIMARY KEY)"))
    await session.execute(
        text(
            "CREATE TABLE artifacts (id uuid PRIMARY KEY, kind text NOT NULL, "
            "resume_id uuid, content_type text, size_bytes integer, sha256 varchar(64))"
        )
    )
    await session.execute(text("GRANT SELECT ON artifacts TO app_agent"))
    await session.execute(text("REVOKE INSERT, UPDATE, DELETE ON artifacts FROM app_agent"))
    await session.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON artifacts TO app_api"))
    return session


async def test_upgrade_and_safe_downgrade_restore_only_worker_insert(migration_table):
    db = migration_table
    baseline = (True, False, False, False, True, True, True, True)
    assert tuple(await privileges(db)) == baseline
    await run_migration(db, "up")
    assert tuple(await privileges(db)) == (True, True, False, False, True, True, True, True)
    await run_migration(db, "down")
    assert tuple(await privileges(db)) == baseline
    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() "
                "AND table_name='artifacts' AND column_name IN "
                "('resume_version', 'approval_id', 'template_version')"
            )
        )
        == 0
    )


async def test_refused_downgrade_preserves_worker_grant_binding_and_rows(migration_table):
    db = migration_table
    await run_migration(db, "up")
    approval_id, resume_id, artifact_id = uuid4(), uuid4(), uuid4()
    await db.execute(text("INSERT INTO approvals VALUES (:id)"), {"id": approval_id})
    await db.execute(
        text(
            "INSERT INTO artifacts (id, kind, resume_id, content_type, size_bytes, sha256, "
            "resume_version, approval_id, template_version) VALUES "
            "(:id, 'resume_pdf', :resume, 'application/pdf', 1, :sha, 1, :approval, 'synthetic-v1')"
        ),
        {"id": artifact_id, "resume": resume_id, "approval": approval_id, "sha": "a" * 64},
    )
    before = tuple(await privileges(db))
    with pytest.raises(DBAPIError, match="Cannot downgrade: rendered resume artifacts"):
        async with db.begin_nested():
            await run_migration(db, "down")
    assert tuple(await privileges(db)) == before
    row = (
        await db.execute(
            text(
                "SELECT resume_version, approval_id, template_version FROM artifacts WHERE id=:id"
            ),
            {"id": artifact_id},
        )
    ).one()
    assert tuple(row) == (1, approval_id, "synthetic-v1")


async def test_deployed_artifact_privileges(session):
    assert tuple(await privileges(session, "public.artifacts")) == (
        True,
        True,
        False,
        False,
        True,
        True,
        True,
        True,
    )
