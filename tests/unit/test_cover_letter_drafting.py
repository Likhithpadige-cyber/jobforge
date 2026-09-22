"""M10 Phase 1 cover-letter drafting: safety and deterministic replay.

No real model and no database - mirrors `tests/unit/test_summary_drafting.py`'s
shape exactly, since `packages.agents.cover_letter.drafting` is B3's sibling,
not a fork.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from packages.agents.base.pii import PiiInPromptError
from packages.agents.cover_letter.drafting import (
    NumeralGuardError,
    UnapprovedFactError,
    draft_cover_letter,
    prepare_request,
    validate_draft,
)
from packages.agents.llm.runtime import (
    ModelOutputError,
    Prices,
    Response,
    StructuredClient,
    canonical,
)
from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.core.models import (
    AgentRun,
    AgentRunStep,
    Event,
    FactStatus,
    FactType,
    LlmCall,
    Recommendation,
)
from packages.shared.config import Settings
from packages.shared.ids import uuid7

pytestmark = pytest.mark.unit


@pytest.fixture
def context():
    facts = (
        TailoringFact(
            id=UUID(int=1),
            text="AWS infrastructure across 12 services.",
            structured={"count": 12},
            fact_type=FactType.EXPERIENCE,
            status=FactStatus.VERIFIED,
        ),
        TailoringFact(
            id=UUID(int=2),
            text="Linux systems administration.",
            structured={},
            fact_type=FactType.SKILL,
            status=FactStatus.VERIFIED,
        ),
    )
    return TailoringContext(
        profile_id=UUID(int=10),
        base_resume_id=UUID(int=11),
        base_resume_version=1,
        base_resume_structure={"sections": []},
        facts=facts,
        job={
            "id": UUID(int=12),
            "title": "Cloud Engineer",
            "normalized_title": "cloud_engineer",
            "seniority": "mid",
            "description_raw": "Build systems",
        },
        evaluation_id=UUID(int=13),
        profile_version=1,
        match_matrix={"requirements": []},
        score=Decimal("70"),
        recommendation=Recommendation.MODERATE,
        score_breakdown={},
        rationale="Persisted rationale",
        gaps=(),
    )


@pytest.fixture
def output():
    return {
        "paragraphs": [
            {
                "text": "I have built AWS infrastructure across 12 services.",
                "fact_ids": [str(UUID(int=1))],
                "numerals_used": ["12"],
            },
            {
                "text": "I administer Linux systems day to day.",
                "fact_ids": [str(UUID(int=2))],
                "numerals_used": [],
            },
            {
                "text": "I would welcome the opportunity to bring this AWS depth to your team.",
                "fact_ids": [str(UUID(int=1))],
                "numerals_used": [],
            },
        ]
    }


@pytest.fixture
def session(monkeypatch):
    from packages.agents.cover_letter import drafting
    from tests.unit.fake_inference_audit import FakeInferenceAudit

    monkeypatch.setattr(drafting, "DomainInferenceAudit", FakeInferenceAudit)
    value = Mock()
    value.flush = AsyncMock()
    return value


def cassette(client, context, output, *, note=None, cost="0.001", stop="tool_use"):
    request = prepare_request(context, model=client.model, note=note)
    response = Response(
        body=canonical(output),
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal(cost),
        stop_reason=stop,
    )
    client.cassette_dir.mkdir(exist_ok=True)
    (client.cassette_dir / f"{request.digest}.json").write_text(response.model_dump_json())
    return request


# --------------------------------------------------------------- pure checks


def test_every_paragraph_requires_a_citation(context):
    """`fact_ids` has `min_length=1` on the schema itself - the structural
    guarantee that no paragraph can be an unverifiable, uncited claim."""
    from pydantic import ValidationError as SchemaValidationError

    from packages.agents.cover_letter.drafting import CoverLetterParagraph

    with pytest.raises(SchemaValidationError):
        CoverLetterParagraph(text="No citation here.", fact_ids=[], numerals_used=[])


def test_grounded_paragraphs_validate(context, output):
    result = validate_draft(canonical(output), context, prompt_version="v1")
    assert len(result.paragraphs) == 3
    assert result.paragraphs[0].fact_ids == [str(UUID(int=1))]


def test_citation_of_an_unverified_fact_is_refused(context, output):
    output["paragraphs"][0]["fact_ids"] = [str(uuid7())]
    with pytest.raises(UnapprovedFactError):
        validate_draft(canonical(output), context, prompt_version="v1")


def test_numeral_absent_from_any_cited_facts_structured_data_is_refused(context, output):
    """The fabrication guard: a numeral with no basis in the cited fact's
    structured payload must never survive into an approvable paragraph."""
    output["paragraphs"][0]["text"] = "I delivered 47 major AWS migrations."
    output["paragraphs"][0]["numerals_used"] = ["47"]
    with pytest.raises(NumeralGuardError):
        validate_draft(canonical(output), context, prompt_version="v1")


def test_numeral_present_in_the_cited_facts_structured_data_is_accepted(context, output):
    validate_draft(canonical(output), context, prompt_version="v1")  # no raise


def test_unredacted_pii_is_refused_before_the_numeral_guard(context, output):
    output["paragraphs"][0]["text"] = "Reach me at jordan.rivera@example.com directly."
    with pytest.raises(PiiInPromptError):
        validate_draft(canonical(output), context, prompt_version="v1")


# ------------------------------------------------------------------ replay


async def test_replay_determinism_and_zero_network_calls(context, output, session, tmp_path):
    transport = AsyncMock(side_effect=AssertionError("Network forbidden"))
    client = StructuredClient(Settings(), cassette_dir=tmp_path, transport=transport)
    cassette(client, context, output)
    a = await draft_cover_letter(session, context=context, client=client)
    b = await draft_cover_letter(session, context=context, client=client)
    assert a == b
    transport.assert_not_called()
    objects = [c.args[0] for c in session.add.call_args_list]
    runs = [o for o in objects if isinstance(o, AgentRun)]
    assert all(r.status == "succeeded" and r.cost_usd == Decimal("0.001") for r in runs)


async def test_revision_note_changes_the_request_digest(context, output, session, tmp_path):
    """A regeneration with reviewer instructions is a different request than
    the first draft - the note must not be silently ignored or cached under
    the same cassette key as a plain draft."""
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    without_note = prepare_request(context, model=client.model)
    with_note = prepare_request(context, model=client.model, note="Mention Linux more.")
    assert without_note.digest != with_note.digest


async def test_fabrication_class_error_emits_guardrail_event(context, output, session, tmp_path):
    output["paragraphs"][0]["fact_ids"] = [str(uuid7())]
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(client, context, output)
    with pytest.raises(UnapprovedFactError):
        await draft_cover_letter(session, context=context, client=client)
    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    calls = [o for o in rows if isinstance(o, LlmCall)]
    events = [row for row in rows if isinstance(row, Event)]
    assert run.status == "failed"
    assert len(calls) == 1  # no repair attempt for a fabrication-class failure
    assert len(events) == 1
    assert events[0].payload["kind"] == "fabrication"
    assert events[0].payload["step"] == "cover_letter_draft"
    step = next(row for row in rows if isinstance(row, AgentRunStep))
    assert step.guardrail_results["failure"] == events[0].payload


async def test_malformed_output_gets_one_repair_attempt(context, output, session, tmp_path):
    settings = Settings(llm_mode="record")

    def raw(value):
        return {
            "content": [{"type": "tool_use", "name": "select_content", "input": value}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "stop_reason": "tool_use",
        }

    transport = AsyncMock(side_effect=[raw({"bad": "shape"}), raw(output)])
    client = StructuredClient(
        settings,
        cassette_dir=tmp_path,
        transport=transport,
        prices=Prices(settings.llm_model_tailoring, Decimal("1"), Decimal("2")),
    )
    result = await draft_cover_letter(session, context=context, client=client)
    assert transport.await_count == 2
    assert len(result.paragraphs) == 3


async def test_truncated_response_is_a_model_output_error(context, output, session, tmp_path):
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(client, context, output, stop="max_tokens")
    with pytest.raises(ModelOutputError):
        await draft_cover_letter(session, context=context, client=client)
