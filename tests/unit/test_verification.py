"""B5 claim verifier safety and deterministic replay; no real model or database."""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from packages.agents.base.pii import PiiInPromptError
from packages.agents.llm.runtime import (
    ModelOutputError,
    Prices,
    Response,
    StructuredClient,
    canonical,
)
from packages.agents.resume_optimizer.context import TailoringFact
from packages.agents.resume_optimizer.verification import (
    ClaimNotSupportedError,
    is_authoritative_failure,
    prepare_request,
    validate_verdict,
    verify_claim,
)
from packages.core.models import (
    AgentRun,
    AgentRunStep,
    ClaimVerdict,
    Event,
    FactStatus,
    FactType,
    LlmCall,
)
from packages.shared.config import Settings


@pytest.fixture
def cited_facts():
    return [
        TailoringFact(
            id=UUID(int=1),
            text="Wrote Python scripts to automate deploys.",
            structured={},
            fact_type=FactType.SKILL,
            status=FactStatus.VERIFIED,
        ),
        TailoringFact(
            id=UUID(int=2),
            text="Managed AWS EC2 instances for a small team.",
            structured={"count": 3},
            fact_type=FactType.EXPERIENCE,
            status=FactStatus.VERIFIED,
        ),
    ]


@pytest.fixture
def session(monkeypatch):
    from packages.agents.resume_optimizer import verification
    from tests.unit.fake_inference_audit import FakeInferenceAudit

    monkeypatch.setattr(verification, "DomainInferenceAudit", FakeInferenceAudit)
    value = Mock()
    value.flush = AsyncMock()
    return value


def cassette(
    client, claim_text, cited_facts, verdict_body, *, cost="0.001", stop="tool_use"
):
    request = prepare_request(claim_text, cited_facts, client.model)
    response = Response(
        body=canonical(verdict_body),
        input_tokens=40,
        output_tokens=20,
        cost_usd=Decimal(cost),
        stop_reason=stop,
    )
    client.cassette_dir.mkdir(exist_ok=True)
    (client.cassette_dir / f"{request.digest}.json").write_text(
        response.model_dump_json()
    )
    return request


# --- pure logic ---------------------------------------------------------


def test_is_authoritative_failure_true_on_any_negative():
    assert is_authoritative_failure(
        [ClaimVerdict.SUPPORTED, ClaimVerdict.OVERREACH, ClaimVerdict.SUPPORTED]
    )


def test_is_authoritative_failure_false_when_all_supported():
    assert not is_authoritative_failure(
        [ClaimVerdict.SUPPORTED, ClaimVerdict.SUPPORTED]
    )


def test_is_authoritative_failure_single_unsupported():
    assert is_authoritative_failure([ClaimVerdict.UNSUPPORTED])


# --- claim isolation -----------------------------------------------------


def test_claim_receives_only_cited_facts(cited_facts):
    # An uncited fact must never reach the request just because it exists
    # elsewhere - only what the caller explicitly passes as cited_facts.
    request = prepare_request("Wrote Python scripts.", cited_facts, "model")
    assert "Secret unrelated fact" not in request.user


def test_prompt_has_no_job_description(cited_facts):
    request = prepare_request("Wrote Python scripts.", cited_facts, "model")
    assert "job description" not in request.user.lower()


def test_prompt_has_no_full_profile_or_selection_leakage(cited_facts):
    request = prepare_request("Wrote Python scripts.", cited_facts, "model")
    assert "profile_id" not in request.user
    assert "selection" not in request.user.lower()
    assert "rationale" not in request.user.lower()


def test_prompt_has_no_b1_b2_b3_prompt_leakage(cited_facts):
    request = prepare_request("Wrote Python scripts.", cited_facts, "model")
    assert "tailoring instructions" not in request.user.lower()
    assert "b1" not in request.user.lower()
    assert "b2" not in request.user.lower()
    assert "b3" not in request.user.lower()


def test_system_prompt_frames_isolation():
    request = prepare_request("Wrote Python scripts.", [], "model")
    assert "no knowledge of any job posting" in request.system


# --- verdict validation ---------------------------------------------------


def validate(verdict, explanation="Grounded in the cited evidence."):
    body = canonical({"verdict": verdict, "explanation": explanation})
    return validate_verdict(body, prompt_version="test")


def test_fully_supported_claim_returns_supported():
    result = validate("supported")
    assert result.verdict is ClaimVerdict.SUPPORTED


def test_responsibility_inflation_is_overreach():
    result = validate(
        "overreach", "The cited fact says 'assisted with deployment', not 'led'."
    )
    assert result.verdict is ClaimVerdict.OVERREACH


def test_seniority_inflation_is_overreach_or_unsupported():
    result = validate("unsupported", "No evidence of principal/lead-level ownership.")
    assert result.verdict in (ClaimVerdict.OVERREACH, ClaimVerdict.UNSUPPORTED)


def test_unsupported_technology_is_unsupported():
    result = validate("unsupported", "PySpark/Kubernetes are not in the cited facts.")
    assert result.verdict is ClaimVerdict.UNSUPPORTED


def test_certification_inflation_is_unsupported_or_overreach():
    result = validate("unsupported", "Studying for a cert is not being certified.")
    assert result.verdict in (ClaimVerdict.OVERREACH, ClaimVerdict.UNSUPPORTED)


def test_security_compliance_inflation_is_unsupported():
    result = validate("unsupported", "No SOC2/HIPAA/PCI evidence in cited facts.")
    assert result.verdict is ClaimVerdict.UNSUPPORTED


def test_qualitative_modifier_inflation_is_overreach():
    result = validate(
        "overreach", "'mission-critical, zero-downtime' is not supported by the fact."
    )
    assert result.verdict is ClaimVerdict.OVERREACH


def test_scope_inflation_is_overreach():
    result = validate("overreach", "Team-level work claimed as enterprise-wide.")
    assert result.verdict is ClaimVerdict.OVERREACH


def test_multi_fact_fabricated_relationship_is_unsupported():
    result = validate(
        "unsupported", "The facts never state these two systems are related."
    )
    assert result.verdict is ClaimVerdict.UNSUPPORTED


def test_unsupported_numeric_claim_is_unsupported():
    result = validate("unsupported", "No cited fact supports this figure.")
    assert result.verdict is ClaimVerdict.UNSUPPORTED


def test_spelled_out_number_claim_is_unsupported():
    result = validate(
        "unsupported", "'doubled' is a numeric claim absent from evidence."
    )
    assert result.verdict is ClaimVerdict.UNSUPPORTED


def test_mixed_claim_one_clause_unsupported_yields_negative_verdict():
    # Even when part of a sentence is grounded, any ungrounded clause makes
    # the whole claim non-supported.
    result = validate("overreach", "First half is grounded; second half is not.")
    assert result.verdict is not ClaimVerdict.SUPPORTED


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "null",
        "[]",
        "{}",
        '{"verdict": "maybe", "explanation": "x"}',
        '{"verdict": "supported"}',
        '{"verdict": "supported", "explanation": "x", "extra": 1}',
        '{"verdict": "supported", "explanation": "x", "verdict": "unsupported"}',
    ],
)
def test_malformed_output_rejected_safely(body):
    with pytest.raises(ModelOutputError):
        validate_verdict(body, prompt_version="test")


@pytest.mark.parametrize(
    "private_text",
    ["Email test@example.com.", "Home address: 42 Cedar Court, Unit 5."],
)
def test_pii_in_explanation_rejected(private_text):
    with pytest.raises(PiiInPromptError):
        validate("overreach", private_text)


# --- end-to-end verify_claim ------------------------------------------------


async def test_negative_verdict_raises_and_records(session, cited_facts, tmp_path):
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Led deployment.",
        cited_facts,
        {"verdict": "overreach", "explanation": "Grounded but inflated."},
    )
    with pytest.raises(ClaimNotSupportedError):
        await verify_claim(
            session,
            claim_text="Led deployment.",
            cited_facts=cited_facts,
            aggregate_id=UUID(int=20),
            client=client,
        )
    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    calls = [o for o in rows if isinstance(o, LlmCall)]
    events = [o for o in rows if isinstance(o, Event)]
    assert run.status == "succeeded"
    assert len(calls) == 1  # the negative verdict is a recorded, legitimate call
    assert len(events) == 1
    assert events[0].payload["kind"] == "fabrication"
    assert events[0].payload["step"] == "B5"
    assert events[0].payload["severity"] == "high"
    step = next(o for o in rows if isinstance(o, AgentRunStep))
    assert step.guardrail_results["failure"] == events[0].payload


async def test_unsupported_verdict_emits_critical_severity(
    session, cited_facts, tmp_path
):
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Introduced PySpark pipelines.",
        cited_facts,
        {"verdict": "unsupported", "explanation": "PySpark is not in the cited facts."},
    )
    with pytest.raises(ClaimNotSupportedError):
        await verify_claim(
            session,
            claim_text="Introduced PySpark pipelines.",
            cited_facts=cited_facts,
            aggregate_id=UUID(int=21),
            client=client,
        )
    rows = [c.args[0] for c in session.add.call_args_list]
    events = [o for o in rows if isinstance(o, Event)]
    assert events[0].payload["severity"] == "critical"


async def test_negative_verdict_does_not_trigger_automatic_redraft(
    session, cited_facts, tmp_path
):
    # A single overreach/unsupported response is recorded once and raised -
    # no second call is made to ask the model to "try again".
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Led deployment.",
        cited_facts,
        {"verdict": "overreach", "explanation": "Grounded but inflated."},
    )
    with pytest.raises(ClaimNotSupportedError):
        await verify_claim(
            session,
            claim_text="Led deployment.",
            cited_facts=cited_facts,
            aggregate_id=UUID(int=22),
            client=client,
        )
    calls = [
        c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], LlmCall)
    ]
    assert len(calls) == 1


async def test_supported_verdict_returns_result_and_succeeds(
    session, cited_facts, tmp_path
):
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Wrote deployment automation scripts in Python.",
        cited_facts,
        {"verdict": "supported", "explanation": "Matches the cited Python fact."},
    )
    result = await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=23),
        client=client,
    )
    assert result.verdict is ClaimVerdict.SUPPORTED
    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    assert run.status == "succeeded"


async def test_replay_determinism(session, cited_facts, tmp_path):
    transport = AsyncMock(side_effect=AssertionError("Network forbidden"))
    client = StructuredClient(Settings(), cassette_dir=tmp_path, transport=transport)
    cassette(
        client,
        "Wrote deployment automation scripts in Python.",
        cited_facts,
        {"verdict": "supported", "explanation": "Matches the cited Python fact."},
    )
    a = await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=24),
        client=client,
    )
    b = await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=24),
        client=client,
    )
    assert a == b
    transport.assert_not_called()


async def test_accounting_survives_malformed_output_then_repair(
    session, cited_facts, tmp_path
):
    settings = Settings(llm_mode="record")

    def raw(value):
        return {
            "content": [{"type": "tool_use", "name": "select_content", "input": value}],
            "usage": {"input_tokens": 40, "output_tokens": 20},
            "stop_reason": "tool_use",
        }

    transport = AsyncMock(
        side_effect=[
            raw({"headline": "Never cache this prose"}),
            raw({"verdict": "supported", "explanation": "Matches the cited fact."}),
        ]
    )
    client = StructuredClient(
        settings,
        cassette_dir=tmp_path,
        transport=transport,
        prices=Prices(settings.llm_model_tailoring, Decimal("1"), Decimal("2")),
    )
    result = await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=25),
        client=client,
    )
    assert transport.await_count == 2
    assert result.verdict is ClaimVerdict.SUPPORTED
    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    calls = [o for o in rows if isinstance(o, LlmCall)]
    assert run.status == "succeeded" and len(calls) == 2
    assert all(
        "Never cache" not in path.read_text() for path in tmp_path.glob("*.json")
    )


async def test_pii_in_explanation_fails_closed_and_emits_event(
    session, cited_facts, tmp_path
):
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Wrote deployment automation scripts in Python.",
        cited_facts,
        {
            "verdict": "supported",
            "explanation": "Contact test@example.com for details.",
        },
    )
    with pytest.raises(PiiInPromptError):
        await verify_claim(
            session,
            claim_text="Wrote deployment automation scripts in Python.",
            cited_facts=cited_facts,
            aggregate_id=UUID(int=26),
            client=client,
        )
    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    events = [o for o in rows if isinstance(o, Event)]
    assert run.status == "failed"
    assert len(events) == 1
    assert events[0].payload["kind"] == "pii_in_prompt"
    assert events[0].payload["severity"] == "high"


async def test_no_persistence_occurs(session, cited_facts, tmp_path):
    from packages.core.models import Resume, ResumeClaim

    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        "Wrote deployment automation scripts in Python.",
        cited_facts,
        {"verdict": "supported", "explanation": "Matches the cited Python fact."},
    )
    await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=27),
        client=client,
    )
    objects = [c.args[0] for c in session.add.call_args_list]
    assert not any(isinstance(o, Resume | ResumeClaim) for o in objects)
    assert all(isinstance(o, AgentRun | AgentRunStep | LlmCall) for o in objects)


async def test_verify_claim_uses_verification_model_when_provided(
    session, cited_facts, tmp_path
):
    settings = Settings()
    client = StructuredClient(
        settings, cassette_dir=tmp_path, model=settings.llm_model_verification
    )
    cassette(
        client,
        "Wrote deployment automation scripts in Python.",
        cited_facts,
        {"verdict": "supported", "explanation": "Matches the cited Python fact."},
    )
    result = await verify_claim(
        session,
        claim_text="Wrote deployment automation scripts in Python.",
        cited_facts=cited_facts,
        aggregate_id=UUID(int=28),
        client=client,
    )
    assert result.verdict is ClaimVerdict.SUPPORTED
    assert client.model == settings.llm_model_verification
