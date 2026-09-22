"""Offline M6 corpus: deterministic guards plus scripted verifier enforcement.

This measures safety-contract enforcement, not a live model's semantic accuracy.
"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from packages.agents.base.pii import PiiInPromptError
from packages.agents.llm.runtime import ModelOutputError, Response, StructuredClient, canonical
from packages.agents.resume_optimizer import drafting, selection, verification
from packages.agents.resume_optimizer.assessment import Alignment, assess
from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.agents.resume_optimizer.evidence import FactView
from packages.agents.resume_optimizer.rationale import build_rationale
from packages.agents.resume_optimizer.requirements import extract_requirements
from packages.core.models import ClaimVerdict, FactStatus, FactType, LlmCall, Recommendation
from packages.shared.config import Settings
from tests.unit.fake_inference_audit import FakeInferenceAudit

pytestmark = [pytest.mark.unit, pytest.mark.safety]
CORPUS_PATH = Path(__file__).with_name("grounding_cases.json")


def cases():
    import json

    return json.loads(CORPUS_PATH.read_text())["cases"]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("M6 offline evaluations forbid network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def context_for(case):
    fact = TailoringFact(
        id=UUID(int=1), text=case.get("fact", "Maintained AWS infrastructure."),
        structured=case.get("structured", {}), fact_type=FactType.EXPERIENCE,
        status=FactStatus(case.get("status", "verified")),
    )
    context = TailoringContext(
        profile_id=UUID(int=10), base_resume_id=UUID(int=11), base_resume_version=1,
        base_resume_structure={"sections": []}, facts=(fact,),
        job={"id": UUID(int=12), "title": "Cloud Engineer", "description_raw": ""},
        evaluation_id=UUID(int=13), profile_version=1, match_matrix={"requirements": []},
        score=Decimal("70"), recommendation=Recommendation.MODERATE,
        score_breakdown={}, rationale="Synthetic persisted rationale", gaps=(),
    )
    selected = selection.SelectionResult(
        selections=(selection.Selection(fact_id=str(fact.id), section="experience",
                                        order=1, emphasis="normal"),),
        evaluation_gaps=(), material_gaps=(), requires_human_review=False,
        prompt_version="offline-fixture",
    )
    return context, selected


def drafted_body(case):
    return canonical({"bullets": [{
        "text": case.get("claim", "Maintained AWS infrastructure."),
        "fact_ids": [] if case.get("no_citations") else [str(UUID(int=case.get("citation", 1)))],
        "numerals_used": [],
    }]})


@pytest.mark.parametrize("case", cases(), ids=lambda case: f'{case["category"]}.{case["id"]}')
async def test_grounding_case(case, tmp_path):
    context, selected = context_for(case)
    kind = case["kind"]
    if kind == "draft_guard":
        errors = {cls.__name__: cls for cls in (
            ModelOutputError, PiiInPromptError, drafting.UnapprovedFactError,
            drafting.NumeralGuardError,
        )}
        if case["expected_error"]:
            with pytest.raises(errors[case["expected_error"]]):
                drafting.validate_drafting(drafted_body(case), context, selected,
                                           prompt_version="offline")
        else:
            result = drafting.validate_drafting(drafted_body(case), context, selected,
                                                prompt_version="offline")
            assert result.bullets[0].text == case["claim"]
    elif kind == "schema":
        with pytest.raises(ModelOutputError):
            drafting.validate_drafting(case["body"], context, selected, prompt_version="offline")
    elif kind == "verifier_replay":
        # Passing deterministic guards is not semantic proof. Exercise the actual
        # independent verifier and audit path with an explicitly scripted verdict.
        result = drafting.validate_drafting(drafted_body(case), context, selected,
                                            prompt_version="offline")
        calls = []

        async def transport(request):
            calls.append(request)
            raise AssertionError("Replay must never invoke a live transport")

        client = StructuredClient(Settings(llm_mode="replay", anthropic_api_key=""),
                                  cassette_dir=tmp_path, transport=transport)
        request = verification.prepare_request(case["claim"], list(context.facts), client.model)
        reply = Response(body=canonical({"verdict": case["verdict"],
                                         "explanation": "Scripted corpus verdict."}),
                         input_tokens=40, output_tokens=20, cost_usd=Decimal("0"),
                         stop_reason="tool_use")
        (tmp_path / f"{request.digest}.json").write_text(reply.model_dump_json())
        session = Mock()
        writer = FakeInferenceAudit(session)
        kwargs = {"claim_text": result.bullets[0].text, "cited_facts": list(context.facts),
                  "aggregate_id": context.job["id"], "client": client, "writer": writer}
        if case["verdict"] == "supported":
            verdict = await verification.verify_claim(session, **kwargs)
            assert verdict.verdict == ClaimVerdict.SUPPORTED
        else:
            with pytest.raises(verification.ClaimNotSupportedError) as error:
                await verification.verify_claim(session, **kwargs)
            assert str(error.value.result.verdict) == case["verdict"]
        audited = [call.args[0] for call in session.add.call_args_list]
        assert sum(isinstance(row, LlmCall) for row in audited) == 1
        assert not calls
    elif kind == "prompt":
        safe, _ = selection.prepare_request(context, "offline")
        hostile = replace(context, job={**context.job, "description_raw": case["payload"]})
        request, scan = selection.prepare_request(hostile, "offline")
        assert scan.findings
        assert request.system == safe.system
        assert request.user.count("<untrusted>") == 1
        assert request.user.count("</untrusted>") == 1
        assert "<system>" not in request.user
        assert {str(fact.id) for fact in hostile.facts} == {str(UUID(int=1))}
    elif kind == "assessment":
        from datetime import date

        extracted = extract_requirements(case["description"])
        result = assess(extracted.requirements, [FactView(UUID(int=1), "AWS")],
                        alignments={name: Alignment(True, "explicit fixture comparison")
                                    for name in ("seniority", "domain", "location")},
                        today=date(2026, 1, 1),
                        extraction_confident=not extracted.needs_llm_fallback)
        assert result.status == "insufficient_evidence"
        assert result.breakdown is None
    elif kind == "rationale":
        matrix = {"requirements": [{"requirement_text": "AWS", "necessity": "required",
                                    "status": "met", "fact_ids": ["f1"] if case["cited"] else []}]}
        before = canonical(matrix)
        first = build_rationale(matrix, score=70, recommendation="moderate")
        second = build_rationale(matrix, score=70, recommendation="moderate")
        assert first.as_dict() == second.as_dict()
        assert first.counts["required_met"] == int(case["cited"])
        assert bool(first.strengths) == case["cited"]
        assert canonical(matrix) == before
    elif kind == "m9_classification":
        # M9's classification gate is a pure text/input-type match with no
        # database and no model - the escalate-only sensitive classifier
        # runs first and unconditionally, so a question matching both a
        # sensitive pattern and an ordinary factual one still lands on
        # sensitive (m9-sensitive-beats-factual-pattern exercises exactly
        # that no-de-escalation-path property).
        from packages.core.models.enums import QuestionClass
        from packages.core.services.applications import classify_question

        result = classify_question(case["question_text"], case["input_type"])
        assert result is QuestionClass(case["expected_class"])
    elif kind == "m9_grounding":
        # Grounding never reaches outside the `facts`/profile it is handed -
        # a foreign, deleted or unverified fact is represented here by its
        # simple absence from that list, which is the only way grounding
        # could ever see one; the real cross-profile/status scoping query
        # is proven against PostgreSQL in
        # tests/integration/test_applications.py, not offline.
        from uuid import uuid4

        from packages.core.models.enums import AnswerSource, QuestionClass
        from packages.core.models.facts import ProfileFact
        from packages.core.models.identity import Profile
        from packages.core.services.applications import draft_answer

        profile_fields: dict[str, object] = {
            "id": uuid4(), "user_id": uuid4(), "full_name": "Offline Candidate",
            "email": "offline@example.invalid", "phone": "555-0100",
            "location_city": "Austin", "location_state": "Texas",
            "location_country": "USA", "willing_to_relocate": True,
            "target_roles": [], "accepted_employment_types": None, "links": {},
        }
        profile_fields.update(case.get("profile_overrides", {}))
        profile = Profile(**profile_fields)  # type: ignore[arg-type]
        facts = [
            ProfileFact(id=uuid4(), profile_id=profile.id, fact_type=f["fact_type"],
                       text=f["text"], source_table="certifications", source_id=uuid4(),
                       status="verified")  # type: ignore[arg-type]
            for f in case.get("facts", [])
        ]
        drafted = draft_answer(
            case["question_text"], QuestionClass(case["question_class"]),
            profile=profile, experiences=[], facts=facts,
        )
        assert drafted.source is AnswerSource(case["expected_source"])
        assert (drafted.value is None) == case["expected_value_is_none"]
        assert drafted.requires_human == case["expected_requires_human"]
        if drafted.source is AnswerSource.AGENT:
            assert drafted.fact_ids, "an agent-sourced answer must cite something"
        else:
            assert drafted.fact_ids == [], "only source=agent may carry a citation"
    elif kind == "m9_employment_gate":
        # The candidate-preference gate: never a database, never inferred
        # full-time merely because a posting looks ordinary - only an
        # explicit match may auto-advance.
        from packages.core.services.applications import draft_employment_type_answer

        drafted = draft_employment_type_answer(
            job_employment_type=case["job_employment_type"],
            accepted_employment_types=case["accepted_employment_types"],
        )
        assert drafted.value == case["expected_value"]
        assert drafted.requires_human == case["expected_requires_human"]
        assert drafted.fact_ids == []
    else:
        pytest.fail(f"Unknown corpus case kind: {kind}")


def test_corpus_inventory_and_positive_controls():
    corpus = cases()
    assert len({case["id"] for case in corpus}) == len(corpus)
    assert {case["category"] for case in corpus} == {
        "fabricated_experience", "fabricated_employers", "fabricated_dates",
        "fabricated_certifications", "fabricated_education", "unsupported_skills",
        "work_authorization", "verified_facts", "evidence_citations", "structured_output",
        "prompt_injection", "unknown_evidence", "rationale_grounding", "unsupported_numerals",
        # M9.
        "sensitive_question_auto_answer_attempt",
        "deterministic_sensitive_classifier_vs_deescalation",
        "ambiguous_question_fact_mapping",
        "fabricated_answer_from_absent_fact",
        "foreign_or_deleted_or_unverified_fact",
        "unsupported_numerical_claim",
        "unresolved_question",
        "explicit_accepted_employment_type",
        "explicit_rejected_employment_type",
        "missing_employment_type",
    }
    assert sum(case.get("verdict") == "supported" for case in corpus) >= 5
    assert sum(case.get("verdict") in ("unsupported", "overreach") for case in corpus) >= 5


@pytest.mark.parametrize("body", [
    '{"verdict":"supported","explanation":"x","approved":true}',
    '{"verdict":"unsupported","verdict":"supported","explanation":"x"}',
    '{"verdict":"supported","explanation":42}',
])
def test_verifier_structured_output_cannot_override_contract(body):
    with pytest.raises(ModelOutputError):
        verification.validate_verdict(body, prompt_version="offline")


async def test_missing_replay_never_falls_back_to_a_provider(tmp_path):
    from packages.agents.llm.runtime import ReplayMissError

    calls = []

    async def forbidden(request):
        calls.append(request)
        raise AssertionError("No paid fallback")

    client = StructuredClient(Settings(llm_mode="replay", anthropic_api_key=""),
                              cassette_dir=tmp_path, transport=forbidden)
    context, _ = context_for({})
    request = verification.prepare_request("Maintained AWS.", list(context.facts), client.model)
    with pytest.raises(ReplayMissError):
        await client.complete(request)
    assert calls == []


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped", "missing"])
def test_report_never_passes_missing_or_failed_cases(outcome):
    from scripts.run_offline_evals import summarize

    results = {f'test_grounding_case[{case["category"]}.{case["id"]}]': True
               for case in cases()}
    assert summarize(results)["passed"] is True
    key = next(iter(results))
    if outcome == "missing":
        results.pop(key)
    else:
        results[key] = False
    report = summarize(results)
    assert report["passed"] is False
    if outcome == "missing":
        assert report["missing_cases"]
    else:
        assert report["failed_or_skipped_tests"] == [key]


def test_work_authorization_is_never_part_of_selection_payload():
    context, _ = context_for({"fact": "Work authorization: private status"})
    context = replace(context, facts=(replace(context.facts[0], fact_type=FactType.AUTHORIZATION),))
    request, _ = selection.prepare_request(context, "offline")
    assert "private status" not in request.user
    assert "Work authorization" not in request.user
