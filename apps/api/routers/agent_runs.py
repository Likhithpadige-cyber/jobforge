"""M13 Phase 3: read-only agent activity.

    GET /api/v1/agent-runs              owner + reviewer
    GET /api/v1/agent-runs/summary      owner + reviewer
    GET /api/v1/agent-runs/{id}         owner + reviewer

Run metadata only (see apps/api/schemas/agent_runs.py for what is excluded
and why). Every handler runs in a PostgreSQL `READ ONLY` transaction, and an
unknown run answers 404 after authentication.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from apps.api.deps import DbSession, authenticated_api
from apps.api.routers.review_status import _read_only
from apps.api.schemas.agent_runs import (
    AgentRunDetail,
    AgentRunRead,
    AgentRunStepRead,
    AgentRunSummary,
)
from packages.core.models import AgentName, AgentRun, AgentRunStep, RunStatus
from packages.shared.clock import utc_now
from packages.shared.errors import NotFoundError

router = APIRouter(
    prefix="/agent-runs", tags=["agent-runs"], dependencies=[Depends(authenticated_api)]
)

#: `error_type` is written as an exception class name. Anything else is
#: reported generically so free text can never reach the response.
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,99}")


def _error_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _CLASS_NAME.fullmatch(value) else "Error"


def _read(run: AgentRun) -> AgentRunRead:
    return AgentRunRead(
        id=run.id,
        agent=run.agent,
        pipeline=run.pipeline,
        status=run.status,
        steps_total=run.steps_total,
        steps_completed=run.steps_completed,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_usd=run.cost_usd,
        duration_ms=run.duration_ms,
        error_type=_error_type(run.error_type),
        started_at=run.started_at,
        finished_at=run.finished_at,
        application_id=run.aggregate_id if run.aggregate_type == "application" else None,
    )


@router.get("", response_model=list[AgentRunRead], summary="Agent runs, newest first")
async def list_runs(
    session: DbSession,
    agent: Annotated[AgentName | None, Query()] = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    application_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AgentRunRead]:
    await _read_only(session)
    stmt = select(AgentRun)
    if agent is not None:
        stmt = stmt.where(AgentRun.agent == agent)
    if run_status is not None:
        stmt = stmt.where(AgentRun.status == run_status)
    if application_id is not None:
        stmt = stmt.where(
            AgentRun.aggregate_type == "application", AgentRun.aggregate_id == application_id
        )
    stmt = stmt.order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(limit).offset(offset)
    runs = (await session.scalars(stmt)).all()
    result = [_read(run) for run in runs]
    await session.rollback()
    return result


@router.get("/summary", response_model=AgentRunSummary, summary="Run totals for recent days")
async def run_summary(
    session: DbSession, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> AgentRunSummary:
    await _read_only(session)
    since = utc_now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                AgentRun.status,
                func.count(),
                func.coalesce(func.sum(AgentRun.input_tokens), 0),
                func.coalesce(func.sum(AgentRun.output_tokens), 0),
                func.coalesce(func.sum(AgentRun.cost_usd), 0),
            )
            .where(AgentRun.started_at >= since)
            .group_by(AgentRun.status)
        )
    ).all()
    await session.rollback()
    return AgentRunSummary(
        since=since,
        runs=sum(row[1] for row in rows),
        by_status={str(row[0]): row[1] for row in rows},
        input_tokens=sum(row[2] for row in rows),
        output_tokens=sum(row[3] for row in rows),
        cost_usd=sum((row[4] for row in rows), start=0),
    )


@router.get("/{run_id}", response_model=AgentRunDetail, summary="One run and its step names")
async def get_run(run_id: UUID, session: DbSession) -> AgentRunDetail:
    await _read_only(session)
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise NotFoundError("agent run not found")
    steps = (
        await session.scalars(
            select(AgentRunStep)
            .where(AgentRunStep.run_id == run_id)
            .order_by(AgentRunStep.step_index)
        )
    ).all()
    detail = AgentRunDetail(
        **_read(run).model_dump(),
        steps=[
            AgentRunStepRead(
                step_index=step.step_index,
                step_name=step.step_name,
                status=step.status,
                duration_ms=step.duration_ms,
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step in steps
        ],
    )
    await session.rollback()
    return detail
