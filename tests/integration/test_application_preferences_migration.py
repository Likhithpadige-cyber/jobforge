"""M9 migration 0009: candidate employment-type preference and the
agent-answer evidence guard, checked by real PostgreSQL.

Isolated rollback-only schema throughout, like the M5/M8 migration tests: no
live tables, Alembic revision rows or cluster roles are rewritten, and the
primary development database is never downgraded.
"""
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
        (SQL / f"0009_application_preferences.{direction}.sql").read_text()
    ):
        await db.execute(text(statement))


@pytest.fixture
async def migration_table(session):
    # As in the M8 migration tests, isolate DDL in a rollback-only schema.
    schema = f"m9_{uuid4().hex}"
    await session.execute(text(f'CREATE SCHEMA "{schema}"'))
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await session.execute(text(
        "CREATE TABLE profiles (id uuid PRIMARY KEY, full_name text NOT NULL)"
    ))
    await session.execute(text(
        "CREATE TABLE application_answers (id uuid PRIMARY KEY, "
        "source text NOT NULL, fact_ids uuid[] NOT NULL DEFAULT '{}')"
    ))
    return session


async def test_upgrade_adds_column_and_constraint(migration_table):
    db = migration_table
    await run_migration(db, "up")

    assert await db.scalar(text(
        "SELECT data_type FROM information_schema.columns WHERE "
        "table_schema=current_schema() AND table_name='profiles' "
        "AND column_name='accepted_employment_types'"
    )) == "ARRAY"

    assert await db.scalar(text(
        "SELECT count(*) FROM information_schema.table_constraints WHERE "
        "table_schema=current_schema() AND table_name='application_answers' "
        "AND constraint_name='ck_application_answers_agent_requires_facts'"
    )) == 1


async def test_downgrade_removes_column_and_constraint(migration_table):
    db = migration_table
    await run_migration(db, "up")
    await run_migration(db, "down")

    assert await db.scalar(text(
        "SELECT count(*) FROM information_schema.columns WHERE "
        "table_schema=current_schema() AND table_name='profiles' "
        "AND column_name='accepted_employment_types'"
    )) == 0
    assert await db.scalar(text(
        "SELECT count(*) FROM information_schema.table_constraints WHERE "
        "table_schema=current_schema() AND table_name='application_answers' "
        "AND constraint_name='ck_application_answers_agent_requires_facts'"
    )) == 0


async def test_existing_rows_remain_valid_across_the_upgrade(migration_table):
    """Pre-existing profiles and non-agent answers must not be invalidated.

    `accepted_employment_types` has no server default and no NOT NULL, so an
    existing profile simply reads NULL (unrestricted) after the upgrade - no
    backfill required. A pre-existing 'human'/'derived'/'blank' answer with
    empty fact_ids must not retroactively violate the new agent-only guard.
    """
    db = migration_table
    profile_id, human_answer_id, blank_answer_id = uuid4(), uuid4(), uuid4()
    await db.execute(
        text("INSERT INTO profiles (id, full_name) VALUES (:id, 'Existing Candidate')"),
        {"id": profile_id},
    )
    await db.execute(
        text("INSERT INTO application_answers (id, source) VALUES (:id, 'human')"),
        {"id": human_answer_id},
    )
    await db.execute(
        text("INSERT INTO application_answers (id, source) VALUES (:id, 'blank')"),
        {"id": blank_answer_id},
    )

    await run_migration(db, "up")

    assert await db.scalar(text(
        "SELECT accepted_employment_types FROM profiles WHERE id=:id"
    ), {"id": profile_id}) is None
    # No exception on the ALTER TABLE ... ADD CONSTRAINT above is itself the
    # proof for the pre-existing rows; re-select to confirm they still exist.
    assert await db.scalar(text(
        "SELECT count(*) FROM application_answers WHERE id IN (:h, :b)"
    ), {"h": human_answer_id, "b": blank_answer_id}) == 2


async def test_new_agent_answer_without_facts_is_refused(migration_table):
    db = migration_table
    await run_migration(db, "up")

    with pytest.raises(DBAPIError, match="ck_application_answers_agent_requires_facts"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO application_answers (id, source, fact_ids) "
                    "VALUES (:id, 'agent', '{}')"
                ),
                {"id": uuid4()},
            )


async def test_new_agent_answer_with_facts_succeeds(migration_table):
    db = migration_table
    await run_migration(db, "up")

    answer_id, fact_id = uuid4(), uuid4()
    await db.execute(
        text(
            "INSERT INTO application_answers (id, source, fact_ids) "
            "VALUES (:id, 'agent', ARRAY[:fact]::uuid[])"
        ),
        {"id": answer_id, "fact": fact_id},
    )
    assert await db.scalar(text(
        "SELECT count(*) FROM application_answers WHERE id=:id"
    ), {"id": answer_id}) == 1


async def test_deployed_grants_are_unchanged_by_this_migration(session):
    """This migration adds no new grants; app_agent/app_api keep exactly the
    table-level privileges 0003 already gave them on the two touched tables.
    """
    row = (await session.execute(text(
        "SELECT has_table_privilege('app_agent','profiles','SELECT'), "
        "has_table_privilege('app_agent','profiles','INSERT'), "
        "has_table_privilege('app_agent','profiles','UPDATE'), "
        "has_table_privilege('app_agent','application_answers','SELECT'), "
        "has_table_privilege('app_agent','application_answers','INSERT'), "
        "has_table_privilege('app_agent','application_answers','UPDATE'), "
        "has_table_privilege('app_api','profiles','UPDATE'), "
        "has_table_privilege('app_api','application_answers','UPDATE')"
    ))).one()
    assert tuple(row) == (True, False, False, True, True, True, True, True)
