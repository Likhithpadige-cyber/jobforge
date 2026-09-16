"""B5 per-claim durable inference evidence and semantic-negative outcomes."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from packages.agents.llm.runtime import CostLimitError, Request, Response, canonical
from packages.agents.resume_optimizer import verification
from packages.core.db.audit_boundary import AuditContext
from packages.core.db.inference_audit import (
    IndependentInferenceAudit,
    InferenceAuditUnavailableError,
)
from packages.core.models import ClaimVerdict
from tests.integration import test_resume_revision as c3
from tests.integration.test_selection_audit import client, evidence, fail_update
from tests.unit import test_verification as unit

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("audit_boundary_engine")]
inputs = c3.inputs


@pytest.fixture
def cited_facts():
    return unit.cited_facts.__wrapped__()


@pytest.fixture
def writer():
    return IndependentInferenceAudit(
        context=AuditContext(
            attempt_id=uuid4(), lease_epoch=2, application_id=uuid4(), approval_id=uuid4()
        )
    )


@pytest.fixture
def job_id():
    return uuid4()


def raw(body, *, inputs=100, outputs=50):
    return {
        "content": [{"type": "tool_use", "name": "select_content", "input": body}],
        "usage": {"input_tokens": inputs, "output_tokens": outputs},
        "stop_reason": "tool_use",
    }


def body(verdict="supported"):
    return {"verdict": verdict, "explanation": "Matches the cited evidence."}


async def run(session, job_id, cited_facts, model, writer, *, claim_index=0):
    return await verification.verify_claim(
        session,
        claim_text="Wrote Python scripts.",
        cited_facts=cited_facts,
        aggregate_id=job_id,
        client=model,
        writer=writer,
        claim_index=claim_index,
    )


async def test_stage_and_request_visible_while_claim_model_paused(
    session, engine, job_id, cited_facts, writer, tmp_path
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(request):
        entered.set()
        await release.wait()
        return raw(body())

    model = client(tmp_path, paused)
    request = verification.prepare_request("Wrote Python scripts.", cited_facts, model.model)
    task = asyncio.create_task(run(session, job_id, cited_facts, model, writer))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        runs, steps, calls, _ = await evidence(engine, job_id)
        assert len(runs) == len(steps) == 1 and not calls
        assert runs[0].status == "running"
        assert steps[0].input_digest == request.digest
        assert steps[0].guardrail_results == {
            "claim_index": 0,
            "c3_execution": writer._context.as_payload(),
        }
    finally:
        release.set()
        await task
    await session.rollback()
    runs, steps, calls, _ = await evidence(engine, job_id)
    assert runs[0].status == steps[0].status == "succeeded"
    assert len(calls) == 1 and calls[0].prompt_hash == request.digest
    assert calls[0].prompt_redacted is None
    assert calls[0].response_redacted == canonical(body())


async def test_each_claim_has_distinct_durable_identity(
    session, engine, job_id, cited_facts, writer, tmp_path
):
    model = client(tmp_path, AsyncMock(return_value=raw(body())))
    await run(session, job_id, cited_facts, model, writer, claim_index=0)
    await run(session, job_id, cited_facts, model, writer, claim_index=1)
    await session.rollback()
    runs, steps, calls, _ = await evidence(engine, job_id)
    assert len(runs) == len(steps) == len(calls) == 2
    assert len({run.id for run in runs}) == len({step.id for step in steps}) == 2
    assert {step.guardrail_results["claim_index"] for step in steps} == {0, 1}
    assert {step.guardrail_results["c3_execution"]["attempt_id"] for step in steps} == {
        str(writer._context.attempt_id)
    }
    assert all("Wrote Python" not in str(step.guardrail_results) for step in steps)


@pytest.mark.parametrize("operation", ["begin_stage", "record_request"])
async def test_start_failure_prevents_model(
    session, job_id, cited_facts, writer, tmp_path, monkeypatch, operation
):
    transport = AsyncMock(return_value=raw(body()))
    if operation == "begin_stage":
        monkeypatch.setattr(
            writer, operation, AsyncMock(side_effect=InferenceAuditUnavailableError())
        )
    else:
        fail_update(monkeypatch, "SET prompt_version")
    with pytest.raises(InferenceAuditUnavailableError):
        await run(session, job_id, cited_facts, client(tmp_path, transport), writer)
    transport.assert_not_awaited()


async def test_response_failure_prevents_validation_and_repair(
    session, job_id, cited_facts, writer, tmp_path, monkeypatch
):
    transport = AsyncMock(return_value=raw({"invalid": "private@example.com"}))
    validate = Mock(wraps=verification.validate_verdict)
    monkeypatch.setattr(verification, "validate_verdict", validate)
    fail_update(monkeypatch, "SET input_tokens")
    with pytest.raises(InferenceAuditUnavailableError):
        await run(session, job_id, cited_facts, client(tmp_path, transport, mode="record"), writer)
    assert transport.await_count == 1
    validate.assert_not_called()
    assert not list(tmp_path.glob("*.json"))


async def test_repair_has_distinct_exact_usage_and_digests(
    session, engine, job_id, cited_facts, writer, tmp_path
):
    transport = AsyncMock(
        side_effect=[
            raw({"invalid": "private@example.com"}, inputs=1000, outputs=500),
            raw(body(), inputs=700, outputs=300),
        ]
    )
    model = client(tmp_path, transport, mode="record")
    await run(session, job_id, cited_facts, model, writer)
    original = verification.prepare_request("Wrote Python scripts.", cited_facts, model.model)
    repair = Request(
        **{
            **original.__dict__,
            "user": original.user
            + "\nReturn only the required schema; the previous output was invalid.",
        }
    )
    runs, steps, calls, _ = await evidence(engine, job_id)
    assert [call.prompt_hash for call in calls] == [original.digest, repair.digest]
    assert [(c.input_tokens, c.output_tokens, c.cost_usd) for c in calls] == [
        (1000, 500, Decimal("0.0020")),
        (700, 300, Decimal("0.0013")),
    ]
    assert runs[0].cost_usd == Decimal("0.0033")
    assert steps[0].input_digest == original.digest
    assert calls[0].response_redacted is None
    assert calls[1].response_redacted == canonical(body())
    assert all(c.prompt_redacted is None for c in calls)
    assert all("private@example.com" not in p.read_text() for p in tmp_path.glob("*.json"))


@pytest.mark.parametrize("kind", ["timeout", "cancel"])
async def test_unknown_provider_outcome_has_no_fake_call(
    session, engine, job_id, cited_facts, writer, tmp_path, kind
):
    primary = TimeoutError("timeout") if kind == "timeout" else asyncio.CancelledError("cancel")
    with pytest.raises(type(primary)) as raised:
        await run(
            session, job_id, cited_facts, client(tmp_path, AsyncMock(side_effect=primary)), writer
        )
    assert raised.value is primary
    runs, steps, calls, _ = await evidence(engine, job_id)
    assert len(runs) == len(steps) == 1 and not calls
    assert steps[0].input_digest is not None


async def test_negative_is_completed_inference_and_guardrail_survives_rollback(
    session, engine, job_id, cited_facts, writer, tmp_path
):
    model = client(tmp_path, AsyncMock(return_value=raw(body("unsupported"))))
    with pytest.raises(verification.ClaimNotSupportedError) as raised:
        await run(session, job_id, cited_facts, model, writer)
    assert raised.value.result.verdict is ClaimVerdict.UNSUPPORTED
    await session.rollback()
    runs, steps, calls, events = await evidence(engine, job_id)
    assert len(runs) == len(steps) == len(calls) == len(events) == 1
    assert runs[0].status == steps[0].status == "succeeded"
    assert runs[0].error_type is None and runs[0].steps_completed == 1
    assert steps[0].output == body("unsupported")
    assert events[0].payload == steps[0].guardrail_results["failure"]
    assert events[0].payload["kind"] == "fabrication"
    assert events[0].correlation_id == job_id and events[0].actor_id == "resume_optimizer"


@pytest.mark.parametrize("fragment", ["SET response_redacted", "SET output"])
async def test_output_failure_stops_claim(
    session, job_id, cited_facts, writer, tmp_path, monkeypatch, fragment
):
    transport = AsyncMock(return_value=raw(body()))
    fail_update(monkeypatch, fragment)
    with pytest.raises(InferenceAuditUnavailableError):
        await run(session, job_id, cited_facts, client(tmp_path, transport), writer)
    assert transport.await_count == 1


async def test_negative_guardrail_failure_cannot_reach_handoff(
    session, job_id, cited_facts, writer, tmp_path, monkeypatch
):
    fail_update(monkeypatch, "SET guardrail_results")
    model = client(tmp_path, AsyncMock(return_value=raw(body("unsupported"))))
    with pytest.raises(InferenceAuditUnavailableError):
        await run(session, job_id, cited_facts, model, writer)


async def test_per_claim_budget_ceiling(session, engine, job_id, cited_facts, writer, tmp_path):
    model = client(tmp_path, AsyncMock(), mode="replay", budget=Decimal("0.001"))
    request = verification.prepare_request("Wrote Python scripts.", cited_facts, model.model)
    (tmp_path / f"{request.digest}.json").write_text(
        Response(
            body=canonical(body()),
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.0020"),
            stop_reason="tool_use",
        ).model_dump_json()
    )
    with pytest.raises(CostLimitError):
        await run(session, job_id, cited_facts, model, writer)
    runs, _, calls, _ = await evidence(engine, job_id)
    assert runs[0].cost_usd == Decimal("0.0020")
    assert len(calls) == 1 and calls[0].response_redacted is None


async def test_b5_evidence_survives_later_t3_rollback(
    session, engine, demo_profile, demo_job, demo_application, inputs, tmp_path, monkeypatch
):
    from packages.core.services import resume_revision

    _, _, facts = inputs
    actor = await c3._changes_requested(session, demo_profile, demo_job, demo_application, facts)
    monkeypatch.setattr(
        resume_revision, "persist_resume", AsyncMock(side_effect=RuntimeError("B6 stopped"))
    )
    with pytest.raises(RuntimeError, match="B6 stopped"):
        await c3._revise(
            session,
            tmp_path,
            demo_application,
            actor,
            bullet_text="Delivered AWS infrastructure work.",
        )
    await session.rollback()
    runs, steps, calls, _ = await evidence(engine, demo_job)
    b5 = [step for step in steps if step.step_name == "B5"]
    assert len(b5) == 3
    assert {step.guardrail_results["claim_index"] for step in b5} == {0, 1, 2}
    assert all(
        next(run for run in runs if run.id == step.run_id).status == "succeeded" for step in b5
    )
    assert len(calls) == len(steps) == 6


async def test_negative_outcome_write_failure_is_technical_failure(
    session, engine, job_id, cited_facts, writer, tmp_path, monkeypatch
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.core.models import Event

    fail_update(monkeypatch, "SET status")
    model = client(tmp_path, AsyncMock(return_value=raw(body("unsupported"))))
    with pytest.raises(InferenceAuditUnavailableError):
        await run(session, job_id, cited_facts, model, writer)
    runs, steps, calls, events = await evidence(engine, job_id)
    assert len(runs) == len(steps) == len(calls) == len(events) == 1
    assert runs[0].status == "running"  # final outcome could not be committed
    async with async_sessionmaker(engine)() as db:
        diagnostics = (
            await db.scalars(
                select(Event).where(
                    Event.aggregate_id == writer._context.application_id,
                    Event.type == "inference.audit.outcome_not_recorded",
                )
            )
        ).all()
    assert len(diagnostics) == 1


async def test_each_claim_has_its_own_spend_ledger(
    session, engine, job_id, cited_facts, writer, tmp_path
):
    model = client(tmp_path, AsyncMock(), mode="replay", budget=Decimal("0.001"))
    request = verification.prepare_request("Wrote Python scripts.", cited_facts, model.model)
    (tmp_path / f"{request.digest}.json").write_text(
        Response(
            body=canonical(body()),
            input_tokens=40,
            output_tokens=20,
            cost_usd=Decimal("0.0008"),
            stop_reason="tool_use",
        ).model_dump_json()
    )
    await run(session, job_id, cited_facts, model, writer, claim_index=0)
    await run(session, job_id, cited_facts, model, writer, claim_index=1)
    runs, _, calls, _ = await evidence(engine, job_id)
    assert len(runs) == len(calls) == 2
    assert all(run.status == "succeeded" and run.cost_usd == Decimal("0.0008") for run in runs)


async def test_guardrail_cancellation_propagates_without_finalization(
    session, engine, job_id, cited_facts, writer, tmp_path, monkeypatch
):
    cancellation = asyncio.CancelledError("guardrail persistence cancelled")
    guardrail = AsyncMock(side_effect=cancellation)
    finish = AsyncMock(side_effect=InferenceAuditUnavailableError("writer stopped"))
    monkeypatch.setattr(writer, "record_guardrail", guardrail)
    monkeypatch.setattr(writer, "finish_stage", finish)
    transport = AsyncMock(return_value=raw(body("unsupported")))

    with pytest.raises(asyncio.CancelledError) as raised:
        await run(session, job_id, cited_facts, client(tmp_path, transport), writer)

    assert raised.value is cancellation
    guardrail.assert_awaited_once()
    finish.assert_not_awaited()
    assert transport.await_count == 1
    runs, steps, calls, _ = await evidence(engine, job_id)
    assert len(runs) == len(steps) == len(calls) == 1
    assert calls[0].input_tokens == 100 and calls[0].output_tokens == 50
    assert runs[0].input_tokens == 100 and runs[0].output_tokens == 50
