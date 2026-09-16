"""Real PostgreSQL proof of writer atomicity, durability and granted columns."""

from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from packages.core.db.audit_boundary import INFERENCE_AUDIT_EVENTS, AuditBoundary, AuditContext
from packages.core.db.audit_session import dispose_audit_engine, init_audit_engine
from packages.core.db.inference_audit import (
    CallHandle,
    DomainInferenceAudit,
    IndependentInferenceAudit,
    InferenceAuditUnavailableError,
    StageHandle,
)
from packages.core.events.envelope import EventEnvelope
from packages.shared.config import Settings
from tests.integration.conftest import _url

pytestmark = pytest.mark.integration


def context():
    return AuditContext(
        attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4(), lease_epoch=3
    )


async def begin(writer, **overrides):
    args = {
        "agent": "resume_optimizer",
        "pipeline": "tailoring",
        "step_name": "B1",
        "aggregate_type": "job",
        "aggregate_id": uuid4(),
        "correlation_id": uuid4(),
    }
    args.update(overrides)
    return await writer.begin_stage(**args)


async def call(writer, stage, **overrides):
    args = {
        "model": "test-model",
        "input_tokens": 20,
        "output_tokens": 10,
        "cost_usd": Decimal("0.0020"),
        "prompt_hash": "a" * 64,
        "stop_reason": "tool_use",
    }
    args.update(overrides)
    return await writer.record_call(stage, **args)


async def row(engine, table, row_id):
    # Fixed table names in test code, never user input.
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        )
        return result.mappings().one_or_none()


@pytest_asyncio.fixture
async def writer(audit_boundary_engine):
    return IndependentInferenceAudit(context=context())


def violation():
    job = uuid4()
    return EventEnvelope(
        type="guardrail.violation.detected",
        aggregate_type="job",
        aggregate_id=job,
        correlation_id=job,
        actor_type="agent",
        actor_id="resume_optimizer",
        payload={
            "kind": "fabrication",
            "agent": "resume_optimizer",
            "severity": "critical",
            "aggregate_type": "job",
            "aggregate_id": str(job),
            "detail": "ungrounded selection",
            "action_taken": "run_failed",
        },
    )


async def exercise_writer(writer, engine):
    stage = await begin(writer)
    await writer.record_request(
        stage, prompt_version="v1", input_digest="b" * 64, guardrail_results={"checks": []}
    )
    first = await call(writer, stage)
    await writer.record_call_output(stage, first, response_redacted="null")
    second = await call(writer, stage)
    await writer.record_call_output(stage, second, response_redacted='{"selected": []}')
    await writer.record_step_output(stage, output={"selected": []})
    event = violation()
    await writer.record_guardrail(stage, guardrail_results={"failure": event.payload}, event=event)
    await writer.finish_stage(
        stage,
        status="failed",
        steps_completed=0,
        duration_ms=11,
        error_type="UngroundedSelectionError",
        error_message="redacted",
    )
    assert writer.spent(stage) == Decimal("0.0040")
    run = await row(engine, "agent_runs", stage.run_id)
    step = await row(engine, "agent_run_steps", stage.step_id)
    saved_event = await row(engine, "events", event.id)
    assert run["input_tokens"] == 40 and run["output_tokens"] == 20
    assert run["cost_usd"] == Decimal("0.0040")
    assert run["status"] == step["status"] == "failed"
    assert run["finished_at"] == step["finished_at"]
    assert step["guardrail_results"] == {
        "failure": event.payload,
        "c3_execution": writer._context.as_payload(),
    }
    for key, value in event.as_row().items():
        assert saved_event[key] == value
    async with engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM events WHERE correlation_id = :id"),
            {"id": event.correlation_id},
        )
        assert count == 1
    for handle in (first, second):
        saved_call = await row(engine, "llm_calls", handle.call_id)
        assert saved_call["run_id"] == stage.run_id and saved_call["step_id"] == stage.step_id
        assert saved_call["prompt_redacted"] is None
    return stage


async def test_durable_context_guardrail_once_and_original_plus_repair(writer, engine, session):
    domain_stage = await begin(DomainInferenceAudit(session))
    stage = await exercise_writer(writer, engine)
    await session.rollback()
    assert await row(engine, "agent_runs", domain_stage.run_id) is None
    assert (await row(engine, "agent_runs", stage.run_id))["status"] == "failed"


async def test_all_operations_under_real_app_audit(audit_engine, engine):
    await dispose_audit_engine()
    await init_audit_engine(Settings(audit_database_url=_url("app_audit")))
    try:
        from packages.core.db.audit_session import audit_session_scope

        async with audit_session_scope() as audit_session:
            assert await audit_session.scalar(text("SELECT current_user")) == "app_audit"
        writer = IndependentInferenceAudit(context=context())
        await exercise_writer(writer, engine)
        for event_type in INFERENCE_AUDIT_EVENTS:
            event_id = await AuditBoundary().try_record_inference(
                event_type, context=writer._context, detail={"operation": "privilege-test"}
            )
            assert event_id is not None
            saved = await row(engine, "events", event_id)
            assert saved["type"] == event_type
            assert saved["payload"]["lease_epoch"] == 3
    finally:
        await dispose_audit_engine()


@pytest.mark.parametrize(
    "operation",
    ["record_request", "finish_stage", "record_call", "record_step_output", "record_guardrail"],
)
async def test_zero_step_match_rolls_back_entire_operation(writer, engine, operation):
    stage = await begin(writer)
    # Leave the run present: request/finalization update it BEFORE discovering
    # the missing step. An independent observer must see that first update undone.
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM agent_run_steps WHERE id = :id"), {"id": stage.step_id}
        )
    kwargs = {
        "record_request": {
            "prompt_version": "should-rollback",
            "input_digest": None,
            "guardrail_results": {},
        },
        "finish_stage": {"status": "failed", "steps_completed": 0, "duration_ms": 55},
        "record_call": {
            "model": "test",
            "input_tokens": 1,
            "output_tokens": 2,
            "cost_usd": Decimal("0.1"),
            "prompt_hash": None,
            "stop_reason": None,
        },
        "record_step_output": {"output": {"x": 1}},
        "record_guardrail": {"guardrail_results": {"failure": True}, "event": violation()},
    }[operation]
    with pytest.raises(InferenceAuditUnavailableError):
        await getattr(writer, operation)(stage, **kwargs)
    run = await row(engine, "agent_runs", stage.run_id)
    assert run["status"] == "running" and run["prompt_version"] is None
    assert run["cost_usd"] == 0 and run["finished_at"] is None
    suffix = (
        "start_failed"
        if operation == "record_request"
        else "outcome_not_recorded"
        if operation == "finish_stage"
        else "evidence_not_recorded"
    )
    async with engine.connect() as conn:
        events = (
            (
                await conn.execute(
                    text("SELECT type FROM events WHERE aggregate_id = :id"),
                    {"id": writer._context.application_id},
                )
            )
            .scalars()
            .all()
        )
        assert events == [f"inference.audit.{suffix}"]
    if operation == "record_guardrail":
        assert await row(engine, "events", kwargs["event"].id) is None


async def test_failed_call_retains_spend_without_retry_or_partial_totals(writer, engine):
    stage = await begin(writer)
    with pytest.raises(InferenceAuditUnavailableError):
        await call(writer, stage, model=None)  # INSERT fails after the totals UPDATE.
    assert writer.spent(stage) == Decimal("0.0020")
    with pytest.raises(InferenceAuditUnavailableError, match="do not retry"):
        await call(writer, stage)
    assert writer.spent(stage) == Decimal("0.0020")
    assert (await row(engine, "agent_runs", stage.run_id))["cost_usd"] == 0
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM llm_calls WHERE run_id = :id"), {"id": stage.run_id}
            )
            == 0
        )


@pytest.mark.parametrize("mismatch", ["run", "step", "call", "missing_call"])
async def test_handles_cannot_update_other_evidence(writer, engine, mismatch):
    first, second = await begin(writer), await begin(writer)
    handle = await call(writer, first)
    supplied = {
        "run": StageHandle(second.run_id, first.step_id),
        "step": StageHandle(first.run_id, second.step_id),
    }.get(mismatch, second)
    if mismatch == "missing_call":
        handle = CallHandle(uuid4())
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.record_call_output(supplied, handle, response_redacted="wrong")
    if mismatch != "missing_call":
        assert (await row(engine, "llm_calls", handle.call_id))["response_redacted"] is None


async def test_guardrail_insert_failure_rolls_back_findings(writer, engine):
    stage = await begin(writer)
    event = violation()
    await writer.record_guardrail(stage, guardrail_results={"original": True}, event=event)
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.record_guardrail(stage, guardrail_results={"wrong": True}, event=event)
    assert (await row(engine, "agent_run_steps", stage.step_id))["guardrail_results"] == {
        "original": True,
        "c3_execution": writer._context.as_payload(),
    }


@pytest.mark.parametrize("bad", [{"raw": object()}, {"c3_execution": {"lease_epoch": 99}}])
async def test_bad_metadata_rolls_back_request_and_keeps_context(writer, engine, bad):
    stage = await begin(writer)
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.record_request(
            stage, prompt_version="wrong", input_digest=None, guardrail_results=bad
        )
    assert (await row(engine, "agent_runs", stage.run_id))["prompt_version"] is None
    assert (await row(engine, "agent_run_steps", stage.step_id))["guardrail_results"] == {
        "c3_execution": writer._context.as_payload()
    }


async def test_begin_stage_second_insert_failure_leaves_no_run(writer, engine):
    correlation = uuid4()
    with pytest.raises(InferenceAuditUnavailableError):
        await begin(writer, correlation_id=correlation, step_name=None)
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM agent_runs WHERE correlation_id = :id"),
                {"id": correlation},
            )
            == 0
        )


async def test_domain_writer_savepoint_atomicity_and_outer_rollback(session, engine):
    writer = DomainInferenceAudit(session)
    stage = await begin(writer)
    await writer.record_request(
        stage, prompt_version="original", input_digest=None, guardrail_results={}
    )
    await session.execute(text("DELETE FROM agent_run_steps WHERE id = :id"), {"id": stage.step_id})
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.finish_stage(stage, status="failed", steps_completed=0, duration_ms=5)
    assert (
        await session.scalar(
            text("SELECT status FROM agent_runs WHERE id = :id"), {"id": stage.run_id}
        )
        == "running"
    )
    assert await row(engine, "agent_runs", stage.run_id) is None
    await session.rollback()
    assert await row(engine, "agent_runs", stage.run_id) is None


async def test_spend_survives_failure_to_open_audit_session(writer):
    stage = await begin(writer)
    await dispose_audit_engine()
    with pytest.raises(InferenceAuditUnavailableError):
        await call(writer, stage)
    assert writer.spent(stage) == Decimal("0.0020")
    with pytest.raises(InferenceAuditUnavailableError, match="do not retry"):
        await call(writer, stage)
    assert writer.spent(stage) == Decimal("0.0020")


async def test_uncertain_commit_is_not_retried(writer, engine, monkeypatch):
    from packages.core.db import inference_audit

    stage = await begin(writer)
    original_scope = inference_audit.audit_session_scope
    attempts = 0

    @asynccontextmanager
    async def lost_acknowledgement():
        nonlocal attempts
        attempts += 1
        async with original_scope() as audit_session:
            yield audit_session
        # The DB committed, but the caller cannot know that it did.
        raise ConnectionError("simulated lost acknowledgement")

    monkeypatch.setattr(inference_audit, "audit_session_scope", lost_acknowledgement)
    with pytest.raises(InferenceAuditUnavailableError):
        await call(writer, stage)
    with pytest.raises(InferenceAuditUnavailableError, match="do not retry"):
        await call(writer, stage)
    assert attempts == 1
    assert writer.spent(stage) == Decimal("0.0020")
    assert (await row(engine, "agent_runs", stage.run_id))["cost_usd"] == Decimal("0.0020")
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM llm_calls WHERE run_id = :id"), {"id": stage.run_id}
            )
            == 1
        )


async def test_database_step_run_mismatch_rolls_back_run_update(writer, engine):
    first, second = await begin(writer), await begin(writer)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE agent_run_steps SET run_id = :run, step_index = 2 " "WHERE id = :id"),
            {"run": second.run_id, "id": first.step_id},
        )
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.record_request(
            first, prompt_version="wrong", input_digest=None, guardrail_results={}
        )
    assert (await row(engine, "agent_runs", first.run_id))["prompt_version"] is None


@pytest.mark.parametrize("column", ["run_id", "step_id"])
async def test_call_update_checks_both_database_identities(writer, engine, column):
    first, second = await begin(writer), await begin(writer)
    handle = await call(writer, first)
    async with engine.begin() as conn:
        # Parametrized columns are fixed test constants.
        await conn.execute(
            text(f"UPDATE llm_calls SET {column} = :wrong WHERE id = :id"),  # noqa: S608
            {"wrong": getattr(second, column), "id": handle.call_id},
        )
    with pytest.raises(InferenceAuditUnavailableError):
        await writer.record_call_output(first, handle, response_redacted="wrong")
    assert (await row(engine, "llm_calls", handle.call_id))["response_redacted"] is None
