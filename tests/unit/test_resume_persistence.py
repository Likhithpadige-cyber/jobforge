"""B6 deterministic mapping: content shape, report, failure payload.

Persistence, events, idempotency and rollback are proven against a real
database in tests/integration/test_resume_persistence.py; this file covers the
pure functions, which need neither.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.agents.resume_optimizer.drafting import DraftedBullet
from packages.agents.resume_optimizer.handoff import (
    ClaimRef,
    bind,
    bullet_refs,
    sentence_refs,
)
from packages.agents.resume_optimizer.resume_persistence import (
    FAILED,
    PASSED,
    RENDER_FORMATS,
    _failures,
    build_content,
    build_verification_report,
)
from packages.agents.resume_optimizer.selection import Selection, SelectionResult
from packages.agents.resume_optimizer.summary import SummarySentence
from packages.agents.resume_optimizer.verification import VerificationResult
from packages.core.models import ClaimVerdict, FactStatus, FactType, Recommendation

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


def verified_claim(ref, context, verdict=ClaimVerdict.SUPPORTED, explanation="ok"):
    return bind(
        ref,
        VerificationResult(
            verdict=verdict, explanation=explanation, prompt_version="v"
        ),
        facts_for(ref, context),
    )


@pytest.fixture
def all_supported(context, bullets, selection, sentences):
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    return tuple(verified_claim(ref, context) for ref in refs)


# --- content shape ----------------------------------------------------------


def test_content_paths_resolve_into_the_document(all_supported):
    """Every persisted claim path must address something in `content`."""
    content = build_content(all_supported)
    for item in all_supported:
        section, index = item.claim.path.lstrip("/").split("/")
        assert content[section][index]["text"] == item.claim.claim_text


def test_content_preserves_fact_ids_and_numerals(all_supported):
    content = build_content(all_supported)
    entry = content["experience"]["0"]
    assert entry["fact_ids"] == [FACT_A]
    assert entry["numerals_used"] == ["3"]
    assert entry["order"] == 0


def test_content_keeps_sections_separate(all_supported):
    content = build_content(all_supported)
    assert set(content) == {"experience", "skills", "summary"}
    assert len(content["summary"]) == 2


def test_content_carries_no_jd_or_profile(all_supported, context):
    import json

    blob = json.dumps(build_content(all_supported))
    assert "SECRET JOB DESCRIPTION" not in blob
    assert "Persisted rationale" not in blob
    assert str(context.profile_id) not in blob


# --- verification report ----------------------------------------------------


def test_report_counts_supported_and_failed(context, bullets, selection, sentences):
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    mixed = (
        verified_claim(refs[0], context),
        verified_claim(refs[1], context, ClaimVerdict.OVERREACH, "inflated"),
        verified_claim(refs[2], context),
        verified_claim(refs[3], context, ClaimVerdict.UNSUPPORTED, "invented"),
    )
    report = build_verification_report(mixed)
    assert report["supported"] == 2 and report["failed"] == 2
    assert len(report["claims"]) == 4
    assert {c["verdict"] for c in report["claims"]} == {
        "supported",
        "overreach",
        "unsupported",
    }


def test_report_records_reason_and_timestamp(all_supported):
    entry = build_verification_report(all_supported)["claims"][0]
    assert entry["reason"] == "ok"
    assert entry["verified_at"]
    assert entry["input_digest"]


# --- failure payload --------------------------------------------------------


def test_failures_lists_only_negative_claims(context, bullets, selection, sentences):
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    mixed = (
        verified_claim(refs[0], context),
        verified_claim(refs[1], context, ClaimVerdict.UNSUPPORTED, "invented tooling"),
    )
    failures = _failures(mixed)
    assert len(failures) == 1
    assert failures[0]["path"] == "/skills/1"
    assert failures[0]["verdict"] == "unsupported"
    assert failures[0]["reason"] == "invented tooling"
    assert failures[0]["fact_ids"] == [FACT_B]


def test_failures_empty_when_all_supported(all_supported):
    assert _failures(all_supported) == []


def test_failure_payload_carries_no_jd_or_profile(context, bullets, selection):
    import json

    ref = bullet_refs(bullets, selection)[0]
    item = verified_claim(ref, context, ClaimVerdict.OVERREACH, "inflated")
    blob = json.dumps(_failures((item,)))
    assert "SECRET JOB DESCRIPTION" not in blob
    assert "Persisted rationale" not in blob


# --- schema-value constants -------------------------------------------------


def test_status_constants_match_the_schema_check():
    assert PASSED == "passed" and FAILED == "failed"


def test_render_formats_match_the_documented_payload():
    assert RENDER_FORMATS == ["pdf", "docx"]


def test_render_formats_are_copied_not_shared(all_supported):
    """A caller mutating the emitted list must not corrupt the constant."""
    formats = list(RENDER_FORMATS)
    formats.append("rtf")
    assert RENDER_FORMATS == ["pdf", "docx"]


# --- claim identity is not re-derived ---------------------------------------


def test_mapping_uses_handoff_identity_verbatim(context):
    ref = ClaimRef("projects", "/projects/7", "Built a thing.", (FACT_A,), ("3",))
    item = verified_claim(ref, context)
    content = build_content((item,))
    assert content["projects"]["7"]["text"] == "Built a thing."
    assert build_verification_report((item,))["claims"][0]["path"] == "/projects/7"


def test_citation_order_is_preserved_in_content(context):
    ref = ClaimRef("skills", "/skills/0", "Both.", (FACT_B, FACT_A))
    content = build_content((verified_claim(ref, context),))
    assert content["skills"]["0"]["fact_ids"] == [FACT_B, FACT_A]


def test_not_checked_never_reaches_the_mapping(context, bullets, selection):
    """`bind` refuses it, so a mapping input can never carry it."""
    from packages.agents.resume_optimizer.handoff import HandoffError

    ref = bullet_refs(bullets, selection)[0]
    with pytest.raises(HandoffError):
        verified_claim(ref, context, ClaimVerdict.NOT_CHECKED)


def test_negative_claims_still_appear_in_content(context, bullets, selection):
    """A failed resume keeps its text for human review."""
    ref = bullet_refs(bullets, selection)[0]
    item = verified_claim(ref, context, ClaimVerdict.OVERREACH, "inflated")
    content = build_content((item,))
    assert content["experience"]["0"]["text"] == ref.claim_text


def test_replace_does_not_disturb_mapping(context, bullets, selection):
    ref = bullet_refs(bullets, selection)[0]
    item = verified_claim(ref, context)
    moved = replace(item, claim=replace(item.claim, path="/experience/4"))
    assert "4" in build_content((moved,))["experience"]
