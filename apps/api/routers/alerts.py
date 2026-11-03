"""Minimal operational incident API for the existing shared workspace."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from apps.api.deps import DbSession, RequireOwner, authenticated_api, require_owner
from packages.core.generation_safety import generation_is_paused, resolve_alert
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


class PauseRead(BaseModel):
    generation_paused: bool


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    severity: AlertSeverity
    status: str
    auto_paused: bool
    created_at: datetime
    acknowledged_by: UUID | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    # Deliberately omit raw JSON detail, free-text title and entity identifiers.


@router.get("/generation-pause", response_model=PauseRead)
async def pause_status(session: DbSession) -> PauseRead:
    return PauseRead(generation_paused=await generation_is_paused(session))


@router.get("", response_model=list[AlertRead])
async def active_alerts(session: DbSession) -> list[AlertRead]:
    rows = (
        await session.scalars(
            select(Alert).where(Alert.status != "resolved").order_by(Alert.created_at, Alert.id)
        )
    ).all()
    return [AlertRead.model_validate(row) for row in rows]


@router.get("/{alert_id}", response_model=AlertRead)
async def inspect_alert(alert_id: UUID, session: DbSession) -> AlertRead:
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Alert not found")
    return AlertRead.model_validate(alert)


@router.post("/{alert_id}/resolve", response_model=AlertRead)
async def resolve(alert_id: UUID, session: DbSession, owner: RequireOwner) -> AlertRead:
    alert = await resolve_alert(session, alert_id, actor=owner)
    result = AlertRead.model_validate(alert)
    await session.commit()
    return result
