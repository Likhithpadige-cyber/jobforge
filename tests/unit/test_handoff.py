"""B5 -> B6 handoff binding and the coverage gate. No model, no database."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from packages.agents.llm.runtime import Response, StructuredClient, canonical
from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.agents.resume_optimizer.drafting import DraftedBullet
from packages.agents.resume_optimizer.handoff import (
    ClaimRef,
    HandoffError,
    assert_unique_identities,
    bind,
    bullet_refs,
    canonical_fact_order,
    compute_input_digest,
    normalize_fact_order,
    sentence_refs,
    validate_coverage,
    verify_claim_for_handoff,
)
from packages.agents.resume_optimizer.selection import Selection, SelectionResult
from packages.agents.resume_optimizer.summary import SummarySentence
from packages.agents.resume_optimizer.verification import (
    VerificationResult,
)
from packages.agents.resume_optimizer.verification import (
    prepare_request as prepare_b5_request,
)
from packages.core.models import (
    AgentRun,
    AgentRunStep,
    ClaimVerdict,
    Event,
    FactStatus,
    FactType,
    LlmCall,
    Recommendation,
)
from packages.shared.clock import utc_now
from packages.shared.config import Settings

pytestmark = [pytest.mark.unit, pytest.mark.safety]

FACT_A = str(UUID(int=1))
FACT_B = str(UUID(int=2))


@pytest.fixture
def context():
    facts = (
        TailoringFact(
            id=UUID(int=1),
            text="Wrote Python deployment scripts.",
            structured={"count": 3},
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
            "description_raw": "SECRET JOB DESCRIPTION",
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
def selection():
    return SelectionResult(
        selections=(
            Selection(fact_id=FACT_A, section="experience", order=1, emphasis="normal"),
            Selection(fact_id=FACT_B, section="skills", order=2, emphasis="normal"),
        ),
        evaluation_gaps=(),
        material_gaps=(),
        requires_human_review=False,
        prompt_version="b1-test",
    )


@pytest.fixture
def bullets():
    return (
        DraftedBullet(
            text="Automated deployments across 3 environments.",
            fact_ids=[FACT_A],
            numerals_used=["3"],
        ),
        DraftedBullet(text="Administered Linux systems.", fact_ids=[FACT_B]),
    )


@pytest.fixture
def sentences():
    return (
        SummarySentence(text="Cloud engineer with Python depth.", fact_ids=[FACT_A]),
        SummarySentence(text="Comfortable across Linux estates.", fact_ids=[FACT_B]),
    )


def facts_for(ref, context):
    by_id = {str(f.id): f for f in context.facts}
    return [by_id[fid] for fid in ref.fact_ids]


def verified(ref, context, verdict=ClaimVerdict.SUPPORTED, explanation="ok"):
    return bind(
        ref,
        VerificationResult(
            verdict=verdict, explanation=explanation, prompt_version="v"
        ),
        facts_for(ref, context),
    )


def all_verified(context, bullets, selection, sentences, **kwargs):
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    return tuple(verified(ref, context, **kwargs) for ref in refs)


def covered(results, bullets, sentences, context, selection):
    return validate_coverage(
        results,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )


# --- identity and binding ---------------------------------------------------


def test_supported_claim_produces_bound_handoff_result(context, bullets, selection):
    ref = bullet_refs(bullets, selection)[0]
    item = verified(ref, context)
    assert item.claim.claim_text == "Automated deployments across 3 environments."
    assert item.claim.fact_ids == (FACT_A,)
    assert item.verdict is ClaimVerdict.SUPPORTED
    assert item.verified_at <= utc_now()
    assert item.input_digest


def test_claim_identity_reuses_section_and_json_pointer(context, bullets, selection):
    refs = bullet_refs(bullets, selection)
    assert refs[0].section == "experience" and refs[0].path == "/experience/0"
    assert refs[1].section == "skills" and refs[1].path == "/skills/1"
    assert sentence_refs((SummarySentence(text="x.", fact_ids=[FACT_A]),))[0].key == (
        "summary",
        "/summary/0",
    )


@pytest.mark.parametrize("verdict", [ClaimVerdict.OVERREACH, ClaimVerdict.UNSUPPORTED])
def test_negative_result_is_preserved_structurally(
    context, bullets, selection, verdict
):
    ref = bullet_refs(bullets, selection)[0]
    item = verified(ref, context, verdict=verdict, explanation="inflated scope")
    assert item.verdict is verdict
    assert item.explanation == "inflated scope"
    assert item.is_negative
    assert item.claim.claim_text == ref.claim_text


def test_not_checked_rejected_at_bind(context, bullets, selection):
    ref = bullet_refs(bullets, selection)[0]
    with pytest.raises(HandoffError):
        verified(ref, context, verdict=ClaimVerdict.NOT_CHECKED)


def test_claim_ref_preserves_citation_order_for_presentation(context):
    """Identity shows the order B2/B3 cited; only comparison is canonical."""
    ref = ClaimRef("skills", "/skills/0", "Both.", (FACT_B, FACT_A))
    assert ref.fact_ids == (FACT_B, FACT_A)


def test_digest_is_independent_of_citation_order(context):
    """Same evidence, different sequence: one verification input, one digest."""
    cited = ClaimRef("skills", "/skills/0", "Both.", (FACT_A, FACT_B))
    reordered = ClaimRef("skills", "/skills/0", "Both.", (FACT_B, FACT_A))
    assert compute_input_digest(
        "Both.", facts_for(cited, context)
    ) == compute_input_digest("Both.", facts_for(reordered, context))


def test_canonical_fact_order_is_deterministic(context):
    forward = canonical_fact_order(list(context.facts))
    backward = canonical_fact_order(list(reversed(context.facts)))
    assert [f.id for f in forward] == [f.id for f in backward]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ((FACT_A,), (FACT_A, FACT_B)),  # added
        ((FACT_A, FACT_B), (FACT_A,)),  # removed
        ((FACT_A, FACT_A), (FACT_A,)),  # repeated, not deduplicated
        ((FACT_A,), (FACT_B,)),  # substituted
    ],
)
def test_digest_still_detects_changed_fact_sets(context, left, right):
    """Canonicalizing order must not canonicalize away membership."""
    a = ClaimRef("skills", "/skills/0", "Claim.", left)
    b = ClaimRef("skills", "/skills/0", "Claim.", right)
    assert compute_input_digest(
        "Claim.", facts_for(a, context)
    ) != compute_input_digest("Claim.", facts_for(b, context))


def test_reordered_citation_does_not_read_as_stale(context, selection):
    """The regression: a pure reorder must pass the coverage gate."""
    bullets = (DraftedBullet(text="Both systems.", fact_ids=[FACT_A, FACT_B]),)
    ref = bullet_refs(bullets, selection)[0]
    item = verified(ref, context)
    # B5 ran against (A, B); the claim now cites the same facts as (B, A).
    reordered = replace(item, claim=replace(item.claim, fact_ids=(FACT_B, FACT_A)))
    out = validate_coverage(
        (reordered,),
        bullets=bullets,
        sentences=(),
        context=context,
        selection=selection,
    )
    assert out[0].verdict is ClaimVerdict.SUPPORTED


def test_reordering_helper_cannot_change_membership(context):
    ref = ClaimRef("skills", "/skills/0", "Both.", (FACT_A, FACT_B))
    reordered = normalize_fact_order(ref, (FACT_B, FACT_A))
    assert reordered.fact_ids == (FACT_B, FACT_A)
    # Presentation changed; the verification input did not.
    assert (
        verified(reordered, context).input_digest == verified(ref, context).input_digest
    )
    with pytest.raises(HandoffError):
        normalize_fact_order(ref, (FACT_A,))


# --- the coverage gate ------------------------------------------------------


def test_exact_b2_and_b3_coverage_accepted(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)
    out = covered(results, bullets, sentences, context, selection)
    assert len(out) == 4
    assert [c.claim.path for c in out] == [
        "/experience/0",
        "/skills/1",
        "/summary/0",
        "/summary/1",
    ]


def test_coverage_is_order_independent(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)
    out = covered(tuple(reversed(results)), bullets, sentences, context, selection)
    assert [c.claim.path for c in out] == [
        "/experience/0",
        "/skills/1",
        "/summary/0",
        "/summary/1",
    ]


def test_missing_result_rejected(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)[:-1]
    with pytest.raises(HandoffError, match="no verification result"):
        covered(results, bullets, sentences, context, selection)


def test_partial_coverage_rejected_when_only_bullets_verified(
    context, bullets, selection, sentences
):
    results = tuple(verified(r, context) for r in bullet_refs(bullets, selection))
    with pytest.raises(HandoffError, match="no verification result"):
        covered(results, bullets, sentences, context, selection)


def test_duplicate_result_rejected(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)
    with pytest.raises(HandoffError, match="Duplicate verification result"):
        covered((*results, results[0]), bullets, sentences, context, selection)


def test_orphan_result_rejected(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)
    orphan = verified(
        ClaimRef("experience", "/experience/99", "Never generated.", (FACT_A,)), context
    )
    with pytest.raises(HandoffError, match="does not match any generated claim"):
        covered((*results, orphan), bullets, sentences, context, selection)


def test_duplicate_claim_identity_rejected():
    """Two claims sharing one key would silently drop a verdict."""
    a = ClaimRef("summary", "/summary/0", "First.", (FACT_A,))
    b = ClaimRef("summary", "/summary/0", "Second, same slot.", (FACT_B,))
    with pytest.raises(HandoffError, match="share one identity"):
        assert_unique_identities((a, b))


def test_generated_identities_are_unique_by_construction(
    context, bullets, selection, sentences
):
    """The builders cannot collide: the index disambiguates within a section,
    and no B1 section is named `summary`."""
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    assert len({r.key for r in refs}) == len(refs)
    assert_unique_identities(refs)


def test_bullet_citing_unselected_fact_is_refused(context, selection):
    orphan_bullet = (DraftedBullet(text="Unapproved.", fact_ids=[str(UUID(int=99))]),)
    with pytest.raises(HandoffError, match="never selected"):
        bullet_refs(orphan_bullet, selection)


def test_changed_claim_text_rejected(context, bullets, selection, sentences):
    results = list(all_verified(context, bullets, selection, sentences))
    stale = results[0]
    results[0] = replace(
        stale, claim=replace(stale.claim, claim_text="Something else entirely.")
    )
    with pytest.raises(HandoffError, match="Claim text changed"):
        covered(tuple(results), bullets, sentences, context, selection)


def test_changed_fact_ids_rejected(context, bullets, selection, sentences):
    results = list(all_verified(context, bullets, selection, sentences))
    stale = results[0]
    results[0] = replace(stale, claim=replace(stale.claim, fact_ids=(FACT_B,)))
    with pytest.raises(HandoffError, match="fact_ids changed"):
        covered(tuple(results), bullets, sentences, context, selection)


def test_input_digest_mismatch_rejected(context, bullets, selection, sentences):
    results = list(all_verified(context, bullets, selection, sentences))
    results[0] = replace(results[0], input_digest="0" * 64)
    with pytest.raises(HandoffError, match="stale"):
        covered(tuple(results), bullets, sentences, context, selection)


def test_evidence_edited_after_verification_rejected(
    context, bullets, selection, sentences
):
    """The fact's text is unchanged in the claim but changed underneath it."""
    results = all_verified(context, bullets, selection, sentences)
    edited = replace(context.facts[0], structured={"count": 999})
    mutated = replace(context, facts=(edited, *context.facts[1:]))
    with pytest.raises(HandoffError, match="stale"):
        covered(results, bullets, sentences, mutated, selection)


@pytest.mark.parametrize("status", [FactStatus.RETIRED, FactStatus.DRAFT])
def test_unverified_fact_rejected_at_handoff(
    context, bullets, selection, sentences, status
):
    results = all_verified(context, bullets, selection, sentences)
    retired = replace(context.facts[0], status=status)
    mutated = replace(context, facts=(retired, *context.facts[1:]))
    with pytest.raises(HandoffError, match="no longer verified"):
        covered(results, bullets, sentences, mutated, selection)


def test_deleted_fact_rejected_at_handoff(context, bullets, selection, sentences):
    results = all_verified(context, bullets, selection, sentences)
    mutated = replace(context, facts=context.facts[1:])
    with pytest.raises(HandoffError, match="no longer exists"):
        covered(results, bullets, sentences, mutated, selection)


def test_not_checked_result_rejected_by_gate(context, bullets, selection, sentences):
    results = list(all_verified(context, bullets, selection, sentences))
    results[0] = replace(results[0], verdict=ClaimVerdict.NOT_CHECKED)
    with pytest.raises(HandoffError, match="never verified"):
        covered(tuple(results), bullets, sentences, context, selection)


def test_negative_verdicts_pass_the_gate(context, bullets, selection, sentences):
    """The gate checks binding, not verdicts - B6 decides what a failure means."""
    results = all_verified(
        context, bullets, selection, sentences, verdict=ClaimVerdict.UNSUPPORTED
    )
    out = covered(results, bullets, sentences, context, selection)
    assert all(c.is_negative for c in out)


# --- isolation and no persistence -------------------------------------------


def test_no_jd_or_profile_leakage_in_handoff(context, bullets, selection, sentences):
    payload = canonical(
        [
            {
                "section": c.claim.section,
                "path": c.claim.path,
                "claim_text": c.claim.claim_text,
                "fact_ids": list(c.claim.fact_ids),
                "verdict": str(c.verdict),
                "explanation": c.explanation,
                "digest": c.input_digest,
            }
            for c in all_verified(context, bullets, selection, sentences)
        ]
    )
    assert "SECRET JOB DESCRIPTION" not in payload
    assert "Persisted rationale" not in payload
    assert str(context.profile_id) not in payload


@pytest.fixture
def session():
    value = Mock()
    value.flush = AsyncMock()
    return value


def cassette(client, claim_text, cited, body):
    request = prepare_b5_request(claim_text, cited, client.model)
    client.cassette_dir.mkdir(exist_ok=True)
    (client.cassette_dir / f"{request.digest}.json").write_text(
        Response(
            body=canonical(body),
            input_tokens=40,
            output_tokens=20,
            cost_usd=Decimal("0.001"),
            stop_reason="tool_use",
        ).model_dump_json()
    )


async def test_wrapper_returns_supported_result(
    session, context, bullets, selection, tmp_path
):
    ref = bullet_refs(bullets, selection)[0]
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        ref.claim_text,
        facts_for(ref, context),
        {"verdict": "supported", "explanation": "Matches cited fact."},
    )
    item = await verify_claim_for_handoff(
        session, claim=ref, context=context, aggregate_id=UUID(int=30), client=client
    )
    assert item.verdict is ClaimVerdict.SUPPORTED
    assert item.claim.key == ("experience", "/experience/0")


@pytest.mark.parametrize(
    ("verdict", "severity"),
    [("overreach", "high"), ("unsupported", "critical")],
)
async def test_wrapper_preserves_negative_verdict_and_guardrail_event(
    session, context, bullets, selection, tmp_path, verdict, severity
):
    """The exception is caught, but everything it fired on the way out stands."""
    ref = bullet_refs(bullets, selection)[0]
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        ref.claim_text,
        facts_for(ref, context),
        {"verdict": verdict, "explanation": "Beyond the cited evidence."},
    )
    item = await verify_claim_for_handoff(
        session, claim=ref, context=context, aggregate_id=UUID(int=31), client=client
    )
    assert item.verdict is ClaimVerdict(verdict)
    assert item.explanation == "Beyond the cited evidence."

    rows = [c.args[0] for c in session.add.call_args_list]
    run = next(o for o in rows if isinstance(o, AgentRun))
    events = [o for o in rows if isinstance(o, Event)]
    calls = [o for o in rows if isinstance(o, LlmCall)]
    assert run.status == "failed"  # fail-closed semantics unchanged
    assert len(calls) == 1  # no redraft, no repair
    assert len(events) == 1
    assert events[0].payload["kind"] == "fabrication"
    assert events[0].payload["step"] == "B5"
    assert events[0].payload["severity"] == severity


async def test_wrapper_persists_nothing(session, context, bullets, selection, tmp_path):
    from packages.core.models import Resume, ResumeClaim

    ref = bullet_refs(bullets, selection)[0]
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    cassette(
        client,
        ref.claim_text,
        facts_for(ref, context),
        {"verdict": "supported", "explanation": "Matches cited fact."},
    )
    await verify_claim_for_handoff(
        session, claim=ref, context=context, aggregate_id=UUID(int=32), client=client
    )
    rows = [c.args[0] for c in session.add.call_args_list]
    assert not any(isinstance(o, Resume | ResumeClaim) for o in rows)
    assert all(isinstance(o, AgentRun | AgentRunStep | LlmCall) for o in rows)


def test_validate_coverage_persists_nothing(
    session, context, bullets, selection, sentences
):
    covered(
        all_verified(context, bullets, selection, sentences),
        bullets,
        sentences,
        context,
        selection,
    )
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


def test_verified_claim_is_immutable(context, bullets, selection):
    item = verified(bullet_refs(bullets, selection)[0], context)
    with pytest.raises(FrozenInstanceError):
        item.verdict = ClaimVerdict.SUPPORTED  # type: ignore[misc]
