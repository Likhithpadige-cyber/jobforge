"""B1-to-B3 boundary and required runtime audit persistence only."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.agents.llm.runtime import Response, StructuredClient, canonical
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import UnapprovedFactError
from packages.agents.resume_optimizer.selection import (
    prepare_request as prepare_b1_request,
)
from packages.agents.resume_optimizer.selection import select_content
from packages.agents.resume_optimizer.summary import draft_summary, prepare_request
from packages.core.models import AgentRun, AgentRunStep, Event, Resume
from packages.shared.config import Settings
from packages.shared.ids import uuid7
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = pytest.mark.integration


async def test_b1_to_b3_boundary(session, demo_profile, demo_job, inputs, tmp_path):
    _base, _evaluation, facts = inputs
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    client = StructuredClient(Settings(), cassette_dir=tmp_path)

    # B1: approve only the first verified fact.
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

    resume_count = (await session.execute(select(Resume.id))).all()

    # B3, happy path: draft a 2-sentence summary grounded in the sole approved fact.
    b3_request = prepare_request(context, selection, client.model)
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
    assert len(result.sentences) == 2
    assert (await session.execute(select(Resume.id))).all() == resume_count

    b3_run = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.aggregate_id == demo_job, AgentRun.cost_usd == Decimal("0.001")
            )
        )
    ).scalar_one()
    step = (
        await session.execute(select(AgentRunStep).where(AgentRunStep.run_id == b3_run.id))
    ).scalar_one()
    assert step.step_name == "B3" and b3_run.status == "succeeded"


async def test_b3_rejects_fact_b1_never_approved(session, demo_profile, demo_job, inputs, tmp_path):
    _base, _evaluation, facts = inputs
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    client = StructuredClient(Settings(), cassette_dir=tmp_path)

    from packages.agents.resume_optimizer.selection import Selection, SelectionResult

    selection = SelectionResult(
        selections=(
            Selection(
                fact_id=str(facts[0].id),
                section="experience",
                order=1,
                emphasis="normal",
            ),
        ),
        evaluation_gaps=(),
        material_gaps=(),
        requires_human_review=False,
        prompt_version="b1-test",
    )
    b3_request = prepare_request(context, selection, client.model)
    b3_body = {
        "sentences": [
            {
                "text": "Unapproved claim.",
                "fact_ids": [str(uuid7())],
                "numerals_used": [],
            },
            {
                "text": "Second sentence.",
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
    with pytest.raises(UnapprovedFactError):
        await draft_summary(session, context=context, selection=selection, client=client)
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
    assert any(e.payload.get("step") == "B3" and e.payload["kind"] == "fabrication" for e in events)
