"""Event envelope and catalogue (EVENTS.md §2, §3)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from packages.core.events.envelope import (
    HUMAN_ONLY_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    SUPPORTED_EVENT_VERSIONS,
    EventEnvelope,
    ForbiddenActorError,
    UnknownEventTypeError,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.unit, pytest.mark.safety]


def envelope(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "type": "job.evaluated",
        "aggregate_type": "job",
        "payload": {"job_id": "x"},
        "correlation_id": uuid4(),
        "actor_type": "agent",
    }
    kwargs.update(overrides)
    return EventEnvelope(**kwargs)  # type: ignore[arg-type]


def test_catalogue_matches_the_document() -> None:
    """The registry and EVENTS.md must not drift: an event in the doc that
    cannot be emitted, or vice versa, is a contract break."""
    doc = (ROOT / "docs" / "EVENTS.md").read_text()
    documented = set(re.findall(r"`([a-z_]+\.[a-z_.]+)`", doc))
    documented = {d for d in documented if not d.endswith(".md")}
    missing = KNOWN_EVENT_TYPES - documented
    assert not missing, f"registered but not documented: {sorted(missing)}"


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(UnknownEventTypeError):
        envelope(type="job.invented_by_a_model")


@pytest.mark.parametrize("event_type", sorted(HUMAN_ONLY_EVENT_TYPES))
def test_agent_cannot_emit_a_human_only_event(event_type: str) -> None:
    """An agent-emitted approval.granted would mean an approval was forged."""
    with pytest.raises(ForbiddenActorError):
        envelope(type=event_type, aggregate_type="approval", actor_type="agent")


@pytest.mark.parametrize("event_type", sorted(HUMAN_ONLY_EVENT_TYPES))
def test_human_may_emit_approval_events(event_type: str) -> None:
    assert envelope(type=event_type, aggregate_type="approval", actor_type="human")


def test_defaults_are_populated() -> None:
    result = envelope()
    assert result.id is not None
    assert result.occurred_at.tzinfo is not None
    assert result.version == 1


def test_as_row_covers_every_events_column() -> None:
    row = envelope().as_row()
    expected = {
        "id", "type", "version", "aggregate_type", "aggregate_id",
        "correlation_id", "causation_id", "actor_type", "actor_id",
        "trace_id", "payload", "occurred_at",
    }
    assert set(row) == expected
    assert "published" not in row, "publication state is the relay's business"


def test_delivery_versions_preserve_v1_and_only_allow_rendered_v2():
    assert set(SUPPORTED_EVENT_VERSIONS) == KNOWN_EVENT_TYPES
    assert all(1 in versions for versions in SUPPORTED_EVENT_VERSIONS.values())
    assert SUPPORTED_EVENT_VERSIONS["resume.rendered"] == frozenset({1, 2})
    assert all(versions == frozenset({1}) for name, versions in SUPPORTED_EVENT_VERSIONS.items()
               if name != "resume.rendered")
