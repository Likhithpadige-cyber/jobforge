"""Writer contract checks that need no database or provider."""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.core.db.audit_boundary import (
    INFERENCE_AUDIT_EVENTS,
    ORCHESTRATION_EVENTS,
    AuditBoundary,
    AuditContext,
)
from packages.core.db.inference_audit import (
    IndependentInferenceAudit,
    InferenceAuditUnavailableError,
    StageHandle,
    _json,
)
from packages.core.events import KNOWN_EVENT_TYPES


def context():
    return AuditContext(
        attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4(), lease_epoch=7
    )


def test_separate_inference_allowlist():
    assert {
        "inference.audit.start_failed",
        "inference.audit.evidence_not_recorded",
        "inference.audit.outcome_not_recorded",
    } == INFERENCE_AUDIT_EVENTS
    assert INFERENCE_AUDIT_EVENTS <= KNOWN_EVENT_TYPES
    assert not INFERENCE_AUDIT_EVENTS & ORCHESTRATION_EVENTS


@pytest.mark.parametrize(
    "event", ["application.status.changed", "revision.attempt.recovery_failed"]
)
async def test_inference_boundary_rejects_other_events(event):
    with pytest.raises(ValueError, match="allowlisted inference"):
        await AuditBoundary().try_record_inference(event, context=context())


@pytest.mark.parametrize("value", [object(), uuid4(), Decimal("1"), float("nan")])
def test_strict_json(value):
    with pytest.raises((TypeError, ValueError)):
        _json({"nested": [value]})


def test_lease_is_required():
    with pytest.raises(ValueError, match="lease_epoch"):
        IndependentInferenceAudit(
            context=AuditContext(attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4())
        )


def test_unknown_stage_is_not_zero_spend():
    writer = IndependentInferenceAudit(context=context())
    with pytest.raises(InferenceAuditUnavailableError, match="unknown stage"):
        writer.spent(StageHandle(uuid4(), uuid4()))


async def test_absent_engine_stops_writer_and_secondary_failure_cannot_mask(monkeypatch):
    diagnostic = AsyncMock(side_effect=RuntimeError("secondary"))
    monkeypatch.setattr(AuditBoundary, "try_record_inference", diagnostic)
    writer = IndependentInferenceAudit(context=context())
    args = {
        "agent": "resume_optimizer",
        "pipeline": "tailoring",
        "step_name": "B1",
        "aggregate_type": "job",
        "aggregate_id": uuid4(),
        "correlation_id": uuid4(),
    }
    with pytest.raises(InferenceAuditUnavailableError) as caught:
        await writer.begin_stage(**args)
    assert "Audit engine not initialised" in str(caught.value.__cause__)
    assert diagnostic.await_args.args == ("inference.audit.start_failed",)
    with pytest.raises(InferenceAuditUnavailableError, match="do not retry"):
        await writer.begin_stage(**args)
    assert diagnostic.await_count == 1
