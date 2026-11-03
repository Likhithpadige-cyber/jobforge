"""M7 bounded import, snapshot binding and shared run budget; no network."""

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.agents.llm.runtime import CostLimitError, Response
from packages.agents.resume_optimizer import drafting, selection
from packages.agents.resume_optimizer.first_draft import FirstDraftClient
from packages.core.services.first_draft import fingerprint
from packages.core.services.master_resume import MasterResumeData
from packages.shared.config import Settings
from tests.evals.test_grounding_corpus import context_for

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.mark.parametrize(
    "payload",
    [
        {"source_label": "Synthetic", "sections": [], "profile_id": str(UUID(int=1))},
        {"source_label": "Synthetic", "sections": []},
        {
            "source_label": "Synthetic",
            "sections": [
                {"name": "skills", "entries": [{"text": "AWS", "fact_ids": ["not-a-uuid"]}]}
            ],
        },
        {
            "source_label": "Synthetic",
            "sections": [{"name": "skills", "entries": [{"text": "AWS", "verified": True}]}],
        },
    ],
)
def test_import_rejects_unrecognized_authority_and_malformed_data(payload):
    with pytest.raises(ValidationError):
        MasterResumeData.model_validate(payload)


def test_snapshot_is_deterministic_and_binds_candidate_evidence_and_master():
    context, _ = context_for({})
    original = fingerprint(context)
    assert fingerprint(replace(context)) == original
    changes = [
        {"profile_id": UUID(int=99)},
        {"base_resume_id": UUID(int=99)},
        {"base_resume_version": 2},
        {"evaluation_id": UUID(int=99)},
        {"facts": (replace(context.facts[0], text="Changed source"),)},
        {"job": {**context.job, "description_raw": "Changed job"}},
        {"base_resume_structure": {"sections": [{"name": "projects"}]}},
    ]
    assert all(fingerprint(replace(context, **change)) != original for change in changes)


async def test_budget_carries_across_first_draft_stages_and_is_per_instance(tmp_path):
    context, chosen = context_for({})
    calls = []

    async def forbidden(request):
        calls.append(request)
        raise AssertionError("No live calls")

    settings = Settings(llm_mode="replay", llm_max_cost_usd_per_run=0.01)
    client = FirstDraftClient(settings, cassette_dir=tmp_path, transport=forbidden)
    first, _ = selection.prepare_request(context, client.model)
    second = drafting.prepare_request(context, chosen, client.model)
    response = Response(
        body="{}", input_tokens=5, output_tokens=5, cost_usd=Decimal("0.01"), stop_reason="tool_use"
    )
    for request in (first, second):
        (tmp_path / f"{request.digest}.json").write_text(response.model_dump_json())
    await client.complete(first)
    with pytest.raises(CostLimitError):
        await client.complete(second, spent=Decimal("0"))
    assert client.total_spent == Decimal("0.01")
    assert FirstDraftClient(settings).total_spent == Decimal("0")
    assert not calls
