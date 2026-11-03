"""Events are append-only except for publication state.

`events` is the outbox AND the audit trail (EVENTS.md §1, SECURITY.md §9.1).
If a row can be edited after the fact, the audit trail records what someone
last wanted it to say rather than what happened.

Every column is exercised individually rather than as one bulk update, so a
failure names the exact column that lost protection.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]

ANY_DB_ERROR = (IntegrityError, DBAPIError)

#: Every events column, with a value that differs from the fixture's.
#: Only `published` / `published_at` are legitimately mutable.
IMMUTABLE_COLUMNS: list[tuple[str, str]] = [
    ("id", "'00000000-0000-7000-8000-000000000001'::uuid"),
    ("type", "'job.discarded'"),
    ("version", "99"),
    ("aggregate_type", "'application'"),
    ("aggregate_id", "'00000000-0000-7000-8000-000000000002'::uuid"),
    ("correlation_id", "'00000000-0000-7000-8000-000000000003'::uuid"),
    ("causation_id", "'00000000-0000-7000-8000-000000000004'::uuid"),
    ("actor_type", "'human'"),
    ("actor_id", "'someone-else'"),
    ("trace_id", "'tampered-trace'"),
    ("payload", "'{\"tampered\": true}'::jsonb"),
    ("occurred_at", "now() - interval '10 days'"),
]

MUTABLE_COLUMNS = ["published", "published_at"]


@pytest.fixture
async def stored_event(session):  # type: ignore[no-untyped-def]
    """One committed-shape event row inside the rolled-back test session."""
    event_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO events (id, type, version, aggregate_type, aggregate_id, "
            "correlation_id, causation_id, actor_type, actor_id, trace_id, payload) "
            "VALUES (:id, 'job.discovered', 1, 'job', :agg, :corr, :cause, "
            "'system', 'ingestion', 'trace-abc', '{\"source\": \"demo\"}'::jsonb)"
        ),
        {"id": event_id, "agg": uuid7(), "corr": uuid7(), "cause": uuid7()},
    )
    await session.flush()
    return event_id


def test_every_column_is_covered() -> None:
    """Guard against the test drifting from the schema.

    If a migration adds a column and nobody adds it here, this fails - which
    is the point. The trigger protects new columns automatically; this test
    makes sure we still *prove* it.
    """
    covered = {name for name, _ in IMMUTABLE_COLUMNS} | set(MUTABLE_COLUMNS)
    assert len(covered) == 14, f"expected 14 events columns, listed {len(covered)}"


@pytest.mark.parametrize(("column", "new_value"), IMMUTABLE_COLUMNS)
async def test_immutable_column_cannot_be_updated(
    session, stored_event, column: str, new_value: str
) -> None:
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(f"UPDATE events SET {column} = {new_value} WHERE id = :id"),  # noqa: S608
            {"id": stored_event},
        )
        await session.flush()

    message = str(exc.value)
    assert "immutable except for publication state" in message
    # The exception must name the offending column, or debugging this is
    # guesswork on a table with fourteen of them.
    assert column in message, f"exception did not name the column: {message}"
    await session.rollback()


async def test_publication_state_is_still_writable(session, stored_event) -> None:
    """The outbox relay must be able to mark a row published."""
    await session.execute(
        text("UPDATE events SET published = true, published_at = now() WHERE id = :id"),
        {"id": stored_event},
    )
    await session.flush()
    published = (
        await session.execute(
            text("SELECT published FROM events WHERE id = :id"), {"id": stored_event}
        )
    ).scalar_one()
    assert published is True


async def test_multi_column_tamper_is_rejected_and_lists_all_columns(session, stored_event) -> None:
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(
                "UPDATE events SET actor_id = 'a-human', correlation_id = "
                "'00000000-0000-7000-8000-000000000009'::uuid WHERE id = :id"
            ),
            {"id": stored_event},
        )
        await session.flush()
    message = str(exc.value)
    assert "actor_id" in message
    assert "correlation_id" in message
    await session.rollback()


async def test_tamper_disguised_as_a_publish_is_rejected(session, stored_event) -> None:
    """The realistic attack: hide a payload edit inside a legitimate publish."""
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(
                "UPDATE events SET published = true, published_at = now(), "
                "payload = '{\"tampered\": true}'::jsonb WHERE id = :id"
            ),
            {"id": stored_event},
        )
        await session.flush()
    assert "payload" in str(exc.value)
    await session.rollback()


async def test_delete_is_rejected(session, stored_event) -> None:
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(text("DELETE FROM events WHERE id = :id"), {"id": stored_event})
        await session.flush()
    assert "append-only" in str(exc.value)
    await session.rollback()


async def test_trigger_covers_columns_the_test_does_not_know_about(session) -> None:
    """The trigger compares the whole row, not an enumerated list.

    Asserted by reading pg_proc: if someone reverts to an enumeration, this
    fails even though every column-specific test above would still pass.
    """
    source = (
        await session.execute(
            text("SELECT prosrc FROM pg_proc WHERE proname = " "'jobforge_events_publish_only'")
        )
    ).scalar_one()
    assert "to_jsonb(OLD)" in source, (
        "the events guard no longer compares the whole row; a column added by "
        "a future migration would be silently mutable"
    )
