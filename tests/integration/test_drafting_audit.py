"""B2/B3 independent evidence ordering and durability."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from packages.agents.llm.runtime import Request, Response, canonical
from packages.agents.resume_optimizer import drafting, summary
from packages.core.db.audit_boundary import AuditContext
from packages.core.db.inference_audit import (
    IndependentInferenceAudit,
    InferenceAuditUnavailableError,
)
from tests.integration import test_resume_revision as c3
from tests.integration.test_selection_audit import client, evidence, fail_update
from tests.unit import test_content_drafting as b2
from tests.unit import test_summary_drafting as b3

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("audit_boundary_engine")]
inputs = c3.inputs


@pytest.fixture
def writer():
    return IndependentInferenceAudit(
        context=AuditContext(
            attempt_id=uuid4(), lease_epoch=2, application_id=uuid4(), approval_id=uuid4()
        )
    )


@pytest.fixture(params=["B2", "B3"])
def case(request):
    module = drafting if request.param == "B2" else summary
    unit = b2 if request.param == "B2" else b3
    context = unit.context.__wrapped__()
    context = replace(context, job={**context.job, "id": uuid4()})
    return module, context, unit.selection.__wrapped__(), unit.output.__wrapped__()


def raw(body, *, inputs=100, outputs=50):
    return {
        "content": [{"type": "tool_use", "name": "select_content", "input": body}],
        "usage": {"input_tokens": inputs, "output_tokens": outputs},
        "stop_reason": "tool_use",
    }


async def run(case, session, model, writer):
    module, context, selection, _ = case
    fn = module.draft_content if module is drafting else module.draft_summary
    return await fn(session, context=context, selection=selection, client=model, writer=writer)


async def test_stage_request_visible_during_model_call(session, engine, case, writer, tmp_path):
    module, context, selection, output = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(request):
        entered.set()
        await release.wait()
        return raw(output)

    model = client(tmp_path, paused)
    request = module.prepare_request(context, selection, model.model)
    task = asyncio.create_task(run(case, session, model, writer))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        runs, steps, calls, _ = await evidence(engine, context.job["id"])
        assert len(runs) == len(steps) == 1 and not calls
        assert runs[0].status == "running"
        assert runs[0].prompt_version == request.prompt_version
        assert steps[0].input_digest == request.digest
        assert steps[0].guardrail_results["c3_execution"] == writer._context.as_payload()
    finally:
        release.set()
        await task
    await session.rollback()
    runs, steps, calls, _ = await evidence(engine, context.job["id"])
    assert runs[0].status == steps[0].status == "succeeded"
    assert len(calls) == 1 and calls[0].prompt_hash == request.digest
    assert calls[0].prompt_redacted is None
    assert calls[0].response_redacted == canonical(output)


@pytest.mark.parametrize("operation", ["begin_stage", "record_request"])
async def test_pre_call_failure_stops_model(
    session, case, writer, tmp_path, monkeypatch, operation
):
    _, _, _, output = case
    transport = AsyncMock(return_value=raw(output))
    if operation == "begin_stage":
        monkeypatch.setattr(
            writer, operation, AsyncMock(side_effect=InferenceAuditUnavailableError())
        )
    else:
        fail_update(monkeypatch, "SET prompt_version")
    with pytest.raises(InferenceAuditUnavailableError):
        await run(case, session, client(tmp_path, transport), writer)
    transport.assert_not_awaited()


async def test_response_failure_prevents_validation_and_repair(
    session, case, writer, tmp_path, monkeypatch
):
    module, _, _, _ = case
    transport = AsyncMock(return_value=raw({"invalid": "private@example.com"}))
    validation_name = "validate_drafting" if module is drafting else "validate_summary"
    validation = Mock(wraps=getattr(module, validation_name))
    monkeypatch.setattr(module, validation_name, validation)
    fail_update(monkeypatch, "SET input_tokens")
    with pytest.raises(InferenceAuditUnavailableError):
        await run(case, session, client(tmp_path, transport, mode="record"), writer)
    assert transport.await_count == 1
    validation.assert_not_called()
    assert not list(tmp_path.glob("*.json"))


async def test_repair_has_distinct_exact_usage_and_digests(session, engine, case, writer, tmp_path):
    module, context, selection, output = case
    transport = AsyncMock(
        side_effect=[
            raw({"invalid": "private@example.com"}, inputs=1000, outputs=500),
            raw(output, inputs=700, outputs=300),
        ]
    )
    model = client(tmp_path, transport, mode="record")
    await run(case, session, model, writer)
    original = module.prepare_request(context, selection, model.model)
    repair = Request(
        **{
            **original.__dict__,
            "user": original.user
            + "\nReturn only the required schema; the previous output was invalid.",
        }
    )
    runs, steps, calls, _ = await evidence(engine, context.job["id"])
    assert [c.prompt_hash for c in calls] == [original.digest, repair.digest]
    assert [(c.input_tokens, c.output_tokens, c.cost_usd) for c in calls] == [
        (1000, 500, Decimal("0.0020")),
        (700, 300, Decimal("0.0013")),
    ]
    assert runs[0].cost_usd == Decimal("0.0033")
    assert steps[0].input_digest == original.digest
    assert calls[0].response_redacted is None
    assert calls[1].response_redacted == canonical(output)
    assert all(c.prompt_redacted is None for c in calls)
    assert "private@example.com" not in str(steps[0].output)
    assert all("private@example.com" not in p.read_text() for p in tmp_path.glob("*.json"))


@pytest.mark.parametrize("kind", ["timeout", "cancel"])
async def test_unknown_provider_outcome_has_no_call(session, engine, case, writer, tmp_path, kind):
    _, context, _, _ = case
    primary = TimeoutError("timeout") if kind == "timeout" else asyncio.CancelledError("cancel")
    with pytest.raises(type(primary)) as raised:
        await run(case, session, client(tmp_path, AsyncMock(side_effect=primary)), writer)
    assert raised.value is primary
    runs, steps, calls, _ = await evidence(engine, context.job["id"])
    assert len(runs) == len(steps) == 1 and not calls
    assert steps[0].input_digest is not None


async def test_guardrail_event_survives_rollback_once(session, engine, case, writer, tmp_path):
    module, context, _, output = case
    key = "bullets" if module is drafting else "sentences"
    output[key][0]["fact_ids"] = [str(uuid4())]
    with pytest.raises(drafting.UnapprovedFactError):
        await run(case, session, client(tmp_path, AsyncMock(return_value=raw(output))), writer)
    await session.rollback()
    runs, steps, calls, events = await evidence(engine, context.job["id"])
    assert len(runs) == len(steps) == len(calls) == len(events) == 1
    assert runs[0].status == "failed"
    assert events[0].payload["step"] == ("B2" if module is drafting else "B3")
    assert events[0].payload == steps[0].guardrail_results["failure"]
    assert events[0].correlation_id == context.job["id"]
    assert events[0].actor_id == "resume_optimizer"


@pytest.mark.parametrize("fragment", ["SET response_redacted", "SET output"])
async def test_output_evidence_failure_stops_stage(
    session, case, writer, tmp_path, monkeypatch, fragment
):
    _, _, _, output = case
    transport = AsyncMock(return_value=raw(output))
    fail_update(monkeypatch, fragment)
    with pytest.raises(InferenceAuditUnavailableError):
        await run(case, session, client(tmp_path, transport), writer)
    assert transport.await_count == 1


async def test_stage_local_budget_ceiling(session, engine, case, writer, tmp_path):
    module, context, selection, output = case
    from packages.agents.llm.runtime import CostLimitError

    model = client(tmp_path, AsyncMock(), mode="replay", budget=Decimal("0.001"))
    request = module.prepare_request(context, selection, model.model)
    (tmp_path / f"{request.digest}.json").write_text(
        Response(body=canonical(output), input_tokens=1000, output_tokens=500,
                 cost_usd=Decimal("0.0020"), stop_reason="tool_use").model_dump_json()
    )
    with pytest.raises(CostLimitError):
        await run(case, session, model, writer)
    runs, _, calls, _ = await evidence(engine, context.job["id"])
    assert runs[0].cost_usd == Decimal("0.0020")
    assert len(calls) == 1 and calls[0].response_redacted is None


async def test_b2_b3_survive_later_t3_failure(
    session, engine, demo_profile, demo_job, demo_application, inputs, tmp_path, monkeypatch
):
    from packages.core.services import resume_revision

    _, _, facts = inputs
    actor = await c3._changes_requested(session, demo_profile, demo_job, demo_application, facts)
    monkeypatch.setattr(
        resume_revision,
        "verify_claim_for_handoff",
        AsyncMock(side_effect=RuntimeError("B5 stopped")),
    )
    with pytest.raises(RuntimeError, match="B5 stopped"):
        await c3._revise(
            session, tmp_path, demo_application, actor,
            bullet_text="Delivered AWS infrastructure work."
        )
    await session.rollback()
    runs, steps, calls, _ = await evidence(engine, demo_job)
    assert len(runs) == len(steps) == len(calls) == 3
    assert {step.step_name for step in steps} == {"B1", "B2", "B3"}
    assert all(run.status == "succeeded" for run in runs)
