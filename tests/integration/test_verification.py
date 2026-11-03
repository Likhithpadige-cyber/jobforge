"""B5 claim verifier: real-DB audit persistence only, chained after B1/B3."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.agents.llm.runtime import Response, StructuredClient, canonical
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.selection import (
    prepare_request as prepare_b1_request,
)
from packages.agents.resume_optimizer.selection import select_content
from packages.agents.resume_optimizer.summary import draft_summary
from packages.agents.resume_optimizer.summary import (
    prepare_request as prepare_b3_request,
)
from packages.agents.resume_optimizer.verification import (
    ClaimNotSupportedError,
    verify_claim,
)
from packages.agents.resume_optimizer.verification import (
    prepare_request as prepare_b5_request,
)
from packages.core.models import AgentRun, AgentRunStep, Event, Resume, ResumeClaim
from packages.shared.config import Settings
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = pytest.mark.integration


async def _run_b1_and_b3(session, demo_profile, demo_job, facts, tmp_path, client):
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    b1_request, _ = prepare_b1_request(context, client.model)
    b1_body = {
        "selections": [
            {
                "fact_id": str(facts[0].id),
                "section": "experience",
                "order": 1,
                "emphasis": "normal",
            }
        ]
    }
    b1_response = Response(
        body=canonical(b1_body),
        input_tokens=100,
        output_tokens=20,
        cost_usd=Decimal("0.002"),
        stop_reason="tool_use",
    )
    (tmp_path / f"{b1_request.digest}.json").write_text(b1_response.model_dump_json())
    selection = await select_content(session, context=context, client=client)

    b3_request = prepare_b3_request(context, selection, client.model)
    b3_body = {
        "sentences": [
            {
                "text": "Verified experience delivering work.",
                "fact_ids": [str(facts[0].id)],
                "numerals_used": [],
            },
            {
                "text": "Focused on impactful contributions.",
                "fact_ids": [str(facts[0].id)],
                "numerals_used": [],
            },
        ]
    }
    b3_response = Response(
        body=canonical(b3_body),
        input_tokens=80,
        output_tokens=30,
        cost_usd=Decimal("0.001"),
        stop_reason="tool_use",
    )
    (tmp_path / f"{b3_request.digest}.json").write_text(b3_response.model_dump_json())
    result = await draft_summary(session, context=context, selection=selection, client=client)
    return context, result


async def test_b3_to_b5_boundary(session, demo_profile, demo_job, inputs, tmp_path):
    _base, _evaluation, facts = inputs
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    context, result = await _run_b1_and_b3(session, demo_profile, demo_job, facts, tmp_path, client)

    resume_count = (await session.execute(select(Resume.id))).all()
    claim_count = (await session.execute(select(ResumeClaim.id))).all()

    sentence = result.sentences[0]
    cited = [f for f in context.facts if str(f.id) in sentence.fact_ids]

    b5_request = prepare_b5_request(sentence.text, cited, client.model)
    b5_body = {"verdict": "supported", "explanation": "Matches the cited fact."}
    b5_response = Response(
        body=canonical(b5_body),
        input_tokens=40,
        output_tokens=20,
        cost_usd=Decimal("0.0005"),
        stop_reason="tool_use",
    )
    (tmp_path / f"{b5_request.digest}.json").write_text(b5_response.model_dump_json())

    verified = await verify_claim(
        session,
        claim_text=sentence.text,
        cited_facts=cited,
        aggregate_id=demo_job,
        client=client,
    )
    assert verified.verdict.value == "supported"

    # B5 persists no resume and no resume_claims - only the existing
    # run/step/llm_call audit rows, exactly like B1/B2/B3.
    assert (await session.execute(select(Resume.id))).all() == resume_count
    assert (await session.execute(select(ResumeClaim.id))).all() == claim_count

    b5_run = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.aggregate_id == demo_job,
                AgentRun.cost_usd == Decimal("0.0005"),
            )
        )
    ).scalar_one()
    step = (
        await session.execute(select(AgentRunStep).where(AgentRunStep.run_id == b5_run.id))
    ).scalar_one()
    assert step.step_name == "B5" and b5_run.status == "succeeded"


async def test_b5_negative_verdict_emits_guardrail_event(
    session, demo_profile, demo_job, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    context, result = await _run_b1_and_b3(session, demo_profile, demo_job, facts, tmp_path, client)

    sentence = result.sentences[0]
    cited = [f for f in context.facts if str(f.id) in sentence.fact_ids]
    inflated_claim = "Architected and led the entire delivery organization."

    b5_request = prepare_b5_request(inflated_claim, cited, client.model)
    b5_body = {
        "verdict": "unsupported",
        "explanation": "No cited fact supports this scope or seniority.",
    }
    b5_response = Response(
        body=canonical(b5_body),
        input_tokens=40,
        output_tokens=20,
        cost_usd=Decimal("0.0005"),
        stop_reason="tool_use",
    )
    (tmp_path / f"{b5_request.digest}.json").write_text(b5_response.model_dump_json())

    with pytest.raises(ClaimNotSupportedError):
        await verify_claim(
            session,
            claim_text=inflated_claim,
            cited_facts=cited,
            aggregate_id=demo_job,
            client=client,
        )

    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.aggregate_id == demo_job,
                    Event.type == "guardrail.violation.detected",
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        e.payload.get("step") == "B5"
        and e.payload["kind"] == "fabrication"
        and e.payload["severity"] == "critical"
        for e in events
    )
