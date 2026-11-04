"""M13 Phase 3: agent activity is run metadata only.

Error messages, step outputs, guardrail results and LLM prompts or responses
can carry applicant data or model text; none of them may reach these routes.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import text

from packages.core.models import User
from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api

client_factory = submission_api.client_factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]

PRIVATE_MARKER = "PRIVATE-APPLICANT-TEXT"


@pytest.fixture
async def runs(session, demo_user):
    """Three runs: one failed with a sensitive message, one with steps, one running."""
    application_id = uuid7()
    ids = [uuid7(), uuid7(), uuid7()]
    rows = [
        (ids[0], "job_application", "evaluation", "failed", 2, 1, "TimeoutError", None, None),
        (
            ids[1],
            "resume_optimizer",
            "tailoring",
            "succeeded",
            2,
            2,
            None,
            "application",
            application_id,
        ),
        (ids[2], "resume_optimizer", "tailoring", "running", 1, 0, "not a class name!", None, None),
    ]
    for offset, (rid, agent, pipeline, status, total, done, etype, agg_type, agg_id) in enumerate(
        rows
    ):
        await session.execute(
            text(
                "INSERT INTO agent_runs (id, agent, pipeline, status, steps_total, "
                "steps_completed, input_tokens, output_tokens, cost_usd, duration_ms, "
                "error_type, error_message, aggregate_type, aggregate_id, started_at) "
                "VALUES (:id, :agent, :pipeline, :status, :total, :done, 10, 5, 0.0012, 900, "
                ":etype, :emsg, :agg_type, :agg_id, "
                "now() - make_interval(secs => :offset))"
            ),
            {
                "id": rid,
                "agent": agent,
                "pipeline": pipeline,
                "status": status,
                "total": total,
                "done": done,
                "etype": etype,
                "emsg": PRIVATE_MARKER if etype else None,
                "agg_type": agg_type,
                "agg_id": agg_id,
                "offset": offset,
            },
        )
    for index in range(2):
        await session.execute(
            text(
                "INSERT INTO agent_run_steps (id, run_id, step_index, step_name, status, output, "
                "guardrail_results, duration_ms) VALUES (:id, :run, :i, :name, 'succeeded', "
                "CAST(:out AS jsonb), CAST(:out AS jsonb), 100)"
            ),
            {
                "id": uuid7(),
                "run": ids[1],
                "i": index,
                "name": f"step_{index}",
                "out": json.dumps({"text": PRIVATE_MARKER}),
            },
        )
    await session.commit()
    yield {"ids": ids, "application_id": application_id}
    await session.execute(
        text("DELETE FROM agent_run_steps WHERE run_id = ANY(:ids)"), {"ids": ids}
    )
    await session.execute(text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": ids})
    await session.commit()


async def _reviewer(session) -> UUID:
    reviewer = User(id=uuid7(), email=f"reviewer-{uuid7().hex}@example.invalid", role="reviewer")
    session.add(reviewer)
    await session.commit()
    return reviewer.id


async def test_runs_are_listed_without_any_sensitive_text(
    client_factory, session, runs, demo_user
) -> None:
    for user in (demo_user, await _reviewer(session)):
        client = await client_factory(user)
        async with client:
            res = await client.get("/api/v1/agent-runs", params={"limit": 100})
        assert res.status_code == 200, res.text
        assert PRIVATE_MARKER not in res.text and "error_message" not in res.text
        mine = {row["id"]: row for row in res.json() if UUID(row["id"]) in runs["ids"]}
        assert len(mine) == 3
        failed, linked, running = (mine[str(run_id)] for run_id in runs["ids"])
        assert failed["error_type"] == "TimeoutError"
        assert running["error_type"] == "Error"  # free text is never echoed
        assert linked["application_id"] == str(runs["application_id"])
        assert failed["application_id"] is None
        assert linked["cost_usd"] == "0.0012"


async def test_filters_order_and_pagination(client_factory, runs, demo_user) -> None:
    client = await client_factory(demo_user)
    async with client:
        failed = await client.get("/api/v1/agent-runs", params={"status": "failed", "limit": 100})
        by_app = await client.get(
            "/api/v1/agent-runs", params={"application_id": str(runs["application_id"])}
        )
        bad = await client.get("/api/v1/agent-runs", params={"status": "exploded"})
        too_many = await client.get("/api/v1/agent-runs", params={"limit": 101})
        first = await client.get("/api/v1/agent-runs", params={"limit": 1})
        second = await client.get("/api/v1/agent-runs", params={"limit": 1, "offset": 1})
    assert all(row["status"] == "failed" for row in failed.json())
    assert str(runs["ids"][0]) in {row["id"] for row in failed.json()}
    assert [row["id"] for row in by_app.json()] == [str(runs["ids"][1])]
    assert bad.status_code == 422 and too_many.status_code == 422
    assert first.json()[0]["started_at"] >= second.json()[0]["started_at"]


async def test_detail_lists_step_names_only(client_factory, runs, demo_user) -> None:
    client = await client_factory(demo_user)
    async with client:
        res = await client.get(f"/api/v1/agent-runs/{runs['ids'][1]}")
        missing = await client.get(f"/api/v1/agent-runs/{uuid7()}")
    assert res.status_code == 200, res.text
    assert [step["step_name"] for step in res.json()["steps"]] == ["step_0", "step_1"]
    assert PRIVATE_MARKER not in res.text
    assert "output" not in res.json()["steps"][0] and "guardrail_results" not in res.text
    assert missing.status_code == 404


async def test_summary_counts_recent_runs(client_factory, session, demo_user) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        before = (await client.get("/api/v1/agent-runs/summary", params={"days": 1})).json()
    run_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO agent_runs (id, agent, pipeline, status, input_tokens, output_tokens, "
            "cost_usd) VALUES (:id, 'monitoring', 'digest', 'succeeded', 3, 2, 0.5)"
        ),
        {"id": run_id},
    )
    await session.commit()
    try:
        async with await client_factory(demo_user) as client:
            after = (await client.get("/api/v1/agent-runs/summary", params={"days": 1})).json()
            invalid = await client.get("/api/v1/agent-runs/summary", params={"days": 0})
        assert after["runs"] == before["runs"] + 1
        assert after["by_status"]["succeeded"] == before["by_status"].get("succeeded", 0) + 1
        assert after["input_tokens"] == before["input_tokens"] + 3
        assert float(after["cost_usd"]) == pytest.approx(float(before["cost_usd"]) + 0.5)
        assert invalid.status_code == 422
    finally:
        await session.execute(text("DELETE FROM agent_runs WHERE id = :id"), {"id": run_id})
        await session.commit()
