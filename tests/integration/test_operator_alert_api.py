"""M12 Phase 3 alert read model, history filters and pause count (owner only).

Rows are committed (the API serves them on its own connection) with a
per-test `kind`, so filters isolate them from shared-database residue, and
they are deleted again in teardown. Only synthetic test rows are removed.
"""

import random
import string
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models import Alert
from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]

PRIVATE = "PRIVATE free text that must never be returned"


@pytest.fixture
async def alerts(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    created = []

    async def add(**fields):
        row = Alert(
            id=uuid7(),
            kind=fields.pop("kind"),
            severity=fields.pop("severity", "high"),
            title=PRIVATE,
            detail=fields.pop("detail", {"subject": "secret-subject", "token": "x"}),
            status=fields.pop("status", "open"),
            auto_paused=fields.pop("auto_paused", False),
            **fields,
        )
        async with maker.begin() as db:
            db.add(row)
        created.append(row.id)
        return row

    yield add
    async with maker.begin() as db:
        await db.execute(delete(Alert).where(Alert.id.in_(created)))


def _kind():
    return "zz_test_" + "".join(random.choices(string.ascii_lowercase, k=10))


async def test_read_model_is_fixed_safe_and_navigable(api_sessions, alerts):
    owner = await api_sessions("owner")
    app_id, job_id, run_id = uuid7(), uuid7(), uuid7()
    stalled = await alerts(
        kind="application_stalled",
        severity="medium",
        aggregate_type="application",
        aggregate_id=app_id,
    )
    guardrail = await alerts(kind="fabrication", aggregate_type="job", aggregate_id=job_id)
    system = await alerts(kind=_kind(), aggregate_type="system", aggregate_id=run_id)

    body = {}
    for row in (stalled, guardrail, system):
        response = await owner.get(f"/api/v1/alerts/{row.id}")
        assert response.status_code == 200, response.text
        body[row.id] = response.json()
        text = response.text
        assert "PRIVATE" not in text and "secret-subject" not in text and "token" not in text
        assert "detail" not in body[row.id] and "aggregate_id" not in body[row.id]

    assert body[stalled.id]["title"] == "Application stalled"
    assert body[stalled.id]["application_id"] == str(app_id)
    assert body[stalled.id]["job_id"] is None
    assert body[guardrail.id]["title"] == "Guardrail incident: fabrication"
    assert body[guardrail.id]["job_id"] == str(job_id)
    assert body[guardrail.id]["application_id"] is None
    # Non-navigable aggregates expose nothing.
    assert body[system.id]["title"] == "Operational incident"
    assert body[system.id]["application_id"] is None and body[system.id]["job_id"] is None
    assert str(run_id) not in str(body[system.id])


@pytest.mark.parametrize(
    ("kind", "auto_paused", "requires_human"),
    [
        ("application_stalled", False, False),
        ("worker_heartbeat_missing", False, False),
        ("dead_letter", False, True),
        ("submission_ambiguous", False, True),
        ("fabrication", True, True),
        ("prompt_injection", False, True),
        # A pausing row is human-only whatever its kind says.
        ("approval_backlog", True, True),
    ],
)
async def test_requires_human_mirrors_resolution_policy(
    api_sessions, alerts, kind, auto_paused, requires_human
):
    owner = await api_sessions("owner")
    row = await alerts(kind=kind, auto_paused=auto_paused)
    body = (await owner.get(f"/api/v1/alerts/{row.id}")).json()
    assert body["requires_human"] is requires_human
    assert body["resolution"] is None
    if auto_paused:
        # Do not leave the shared suite paused.
        resolved = await owner.post(f"/api/v1/alerts/{row.id}/resolve")
        assert resolved.status_code == 200


async def test_resolution_source_is_reported(api_sessions, alerts):
    owner = await api_sessions("owner")
    by_monitor = await alerts(
        kind="approval_backlog",
        status="resolved",
        resolved_at=datetime.now(UTC),
        detail={"subject": "approvals", "resolved_by": "monitor:recovered"},
    )
    by_human = await alerts(kind="dead_letter")
    resolved = await owner.post(f"/api/v1/alerts/{by_human.id}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["resolution"] == "human"
    assert (await owner.get(f"/api/v1/alerts/{by_monitor.id}")).json()["resolution"] == "monitor"


async def test_history_filters_and_pagination(api_sessions, alerts):
    owner = await api_sessions("owner")
    kind = _kind()
    base = datetime(2099, 6, 1, tzinfo=UTC)
    open_rows = [
        await alerts(kind=kind, severity="low", created_at=base + timedelta(minutes=i))
        for i in range(3)
    ]
    done = await alerts(
        kind=kind,
        severity="high",
        status="resolved",
        resolved_at=base,
        created_at=base - timedelta(minutes=5),
    )

    async def ids(**params):
        response = await owner.get("/api/v1/alerts", params={"kind": kind, **params})
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()]

    newest_first = [str(row.id) for row in reversed(open_rows)]
    assert await ids() == newest_first  # default: open only
    assert await ids(status="open", limit=2) == newest_first[:2]
    assert await ids(status="open", limit=2, offset=2) == newest_first[2:]
    assert await ids(status="resolved") == [str(done.id)]
    assert await ids(status="all") == [*newest_first, str(done.id)]
    assert await ids(status="all", severity="high") == [str(done.id)]
    assert await ids(status="all", severity="low") == newest_first


async def test_pausing_filter_finds_every_pausing_incident(api_sessions, alerts):
    owner = await api_sessions("owner")
    kind = _kind()
    pausing = await alerts(kind=kind, auto_paused=True, severity="critical")
    other = await alerts(kind=kind)
    try:
        response = await owner.get("/api/v1/alerts", params={"kind": kind, "auto_paused": "true"})
        assert [row["id"] for row in response.json()] == [str(pausing.id)]
        response = await owner.get("/api/v1/alerts", params={"kind": kind, "auto_paused": "false"})
        assert [row["id"] for row in response.json()] == [str(other.id)]
    finally:
        await owner.post(f"/api/v1/alerts/{pausing.id}/resolve")


@pytest.mark.parametrize(
    "params",
    [
        {"status": "closed"},
        {"kind": "Robert'); DROP TABLE alerts;--"},
        {"kind": "x" * 65},
        {"severity": "urgent"},
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
    ],
)
async def test_invalid_filters_are_rejected(api_sessions, params):
    owner = await api_sessions("owner")
    assert (await owner.get("/api/v1/alerts", params=params)).status_code == 422


async def test_pause_count_is_server_authoritative(api_sessions, alerts):
    owner = await api_sessions("owner")
    before = (await owner.get("/api/v1/alerts/generation-pause")).json()
    assert before == {"generation_paused": False, "unresolved_auto_pause_count": 0}
    a = await alerts(kind="fabrication", severity="critical", auto_paused=True)
    b = await alerts(kind="fabrication", severity="critical", auto_paused=True)
    await alerts(kind="dead_letter")  # operational alerts never count
    status = (await owner.get("/api/v1/alerts/generation-pause")).json()
    assert status == {"generation_paused": True, "unresolved_auto_pause_count": 2}
    await owner.post(f"/api/v1/alerts/{a.id}/resolve")
    status = (await owner.get("/api/v1/alerts/generation-pause")).json()
    assert status == {"generation_paused": True, "unresolved_auto_pause_count": 1}
    await owner.post(f"/api/v1/alerts/{b.id}/resolve")
    status = (await owner.get("/api/v1/alerts/generation-pause")).json()
    assert status == {"generation_paused": False, "unresolved_auto_pause_count": 0}


async def test_reviewer_and_anonymous_cannot_read_history_or_count(
    api_sessions, app_client, alerts
):
    reviewer = await api_sessions("reviewer")
    row = await alerts(kind="dead_letter")
    for path in (
        "/api/v1/alerts?status=all",
        "/api/v1/alerts?status=resolved&limit=5",
        "/api/v1/alerts/generation-pause",
        f"/api/v1/alerts/{row.id}",
    ):
        assert (await reviewer.get(path)).status_code == 403
        assert (await app_client.get(path)).status_code == 401
    assert (await reviewer.post(f"/api/v1/alerts/{row.id}/resolve")).status_code == 403


async def test_resolution_still_requires_csrf(api_sessions, alerts):
    from apps.api.deps import CSRF_HEADER

    owner = await api_sessions("owner")
    row = await alerts(kind="dead_letter")
    owner.headers.pop(CSRF_HEADER)
    assert (await owner.post(f"/api/v1/alerts/{row.id}/resolve")).status_code == 403
    owner_again = await api_sessions("owner")
    assert (await owner_again.get(f"/api/v1/alerts/{row.id}")).json()["status"] == "open"
