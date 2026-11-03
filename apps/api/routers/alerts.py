"""Operational incident API for the existing shared workspace (owner only).

M12 Phase 1 added pause status, the open list, detail and human resolution.
M12 Phase 3 extends the read model for the operator UI without exposing more:

* `title` is derived from `kind` here, never read from the stored column, so
  it is always one of a fixed set of strings.
* `application_id` / `job_id` are exposed only for those aggregate types,
  because the UI can navigate to them; other entity identifiers stay hidden.
* `resolution` and `requires_human` describe the existing policy; they do
  not change it. Raw `detail` is never returned.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from apps.api.deps import DbSession, RequireOwner, authenticated_api, require_owner
from packages.core.events.guardrail_alerts import KINDS as GUARDRAIL_KINDS
from packages.core.events.operational_monitoring import AUTO_RESOLVABLE
from packages.core.events.operational_monitoring import TITLE as OPERATIONAL_TITLES
from packages.core.generation_safety import (
    generation_is_paused,
    resolve_alert,
    unresolved_auto_pause_count,
)
from packages.core.models import Alert, AlertSeverity
from packages.shared.errors import NotFoundError

router = APIRouter(
    prefix="/alerts",
    tags=["alerts"],
    dependencies=[
        Depends(authenticated_api),
        Depends(require_owner),
    ],
)

MONITOR_RESOLUTION = "monitor:recovered"


class PauseRead(BaseModel):
    generation_paused: bool
    unresolved_auto_pause_count: int


class AlertRead(BaseModel):
    id: UUID
    kind: str
    title: str
    severity: AlertSeverity
    status: str
    auto_paused: bool
    requires_human: bool
    resolution: Literal["human", "monitor"] | None
    application_id: UUID | None
    job_id: UUID | None
    created_at: datetime
    acknowledged_by: UUID | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    # Deliberately omit raw JSON detail, the stored title and other entity ids.


def _title(kind: str) -> str:
    if kind in OPERATIONAL_TITLES:
        return OPERATIONAL_TITLES[kind]
    if kind in GUARDRAIL_KINDS:
        return f"Guardrail incident: {kind.replace('_', ' ')}"
    return "Operational incident"


def _read(alert: Alert) -> AlertRead:
    detail: dict[str, Any] = alert.detail or {}
    resolution: Literal["human", "monitor"] | None = None
    if alert.status == "resolved":
        resolution = "monitor" if detail.get("resolved_by") == MONITOR_RESOLUTION else "human"
    return AlertRead(
        id=alert.id,
        kind=alert.kind,
        title=_title(alert.kind),
        severity=alert.severity,
        status=alert.status,
        auto_paused=alert.auto_paused,
        # Only non-pausing kinds with a verified recovery rule clear themselves.
        requires_human=alert.auto_paused or alert.kind not in AUTO_RESOLVABLE,
        resolution=resolution,
        application_id=alert.aggregate_id if alert.aggregate_type == "application" else None,
        job_id=alert.aggregate_id if alert.aggregate_type == "job" else None,
        created_at=alert.created_at,
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
        resolved_at=alert.resolved_at,
    )


@router.get("/generation-pause", response_model=PauseRead)
async def pause_status(session: DbSession) -> PauseRead:
    return PauseRead(
        generation_paused=await generation_is_paused(session),
        unresolved_auto_pause_count=await unresolved_auto_pause_count(session),
    )


@router.get("", response_model=list[AlertRead])
async def list_alerts(
    session: DbSession,
    status: Literal["open", "resolved", "all"] = "open",
    kind: str | None = Query(default=None, pattern=r"^[a-z_]{1,64}$"),
    severity: AlertSeverity | None = None,
    auto_paused: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> list[AlertRead]:
    """Newest first. `open` includes acknowledged incidents."""
    query = select(Alert)
    if status == "open":
        query = query.where(Alert.status != "resolved")
    elif status == "resolved":
        query = query.where(Alert.status == "resolved")
    if kind is not None:
        query = query.where(Alert.kind == kind)
    if severity is not None:
        query = query.where(Alert.severity == severity)
    if auto_paused is not None:
        query = query.where(Alert.auto_paused.is_(auto_paused))
    rows = await session.scalars(
        query.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit).offset(offset)
    )
    return [_read(row) for row in rows]


@router.get("/{alert_id}", response_model=AlertRead)
async def inspect_alert(alert_id: UUID, session: DbSession) -> AlertRead:
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Alert not found")
    return _read(alert)


@router.post("/{alert_id}/resolve", response_model=AlertRead)
async def resolve(alert_id: UUID, session: DbSession, owner: RequireOwner) -> AlertRead:
    alert = await resolve_alert(session, alert_id, actor=owner)
    result = _read(alert)
    await session.commit()
    return result
