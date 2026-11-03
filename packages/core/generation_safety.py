"""Durable, system-wide generation authority; no cached pause flag."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Alert, User
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError, ForbiddenError, NotFoundError


class GenerationPausedError(ConflictError):
    title = "Generation Paused"
    error_type = "https://jobforge.local/errors/generation-paused"


async def generation_is_paused(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Alert.auto_paused.is_(True),
                    Alert.status != "resolved",
                )
            )
        )
    )


async def require_generation_enabled(session: AsyncSession | None) -> None:
    # Missing authority fails closed, including direct calls to StructuredClient.
    if session is None:
        raise GenerationPausedError("Generation requires a durable safety-state check")
    if await generation_is_paused(session):
        raise GenerationPausedError("Generation is paused pending human incident resolution")


async def resolve_alert(session: AsyncSession, alert_id: UUID, *, actor: User) -> Alert:
    if actor.role != "owner":
        raise ForbiddenError("Incident resolution requires an owner")
    alert = await session.scalar(select(Alert).where(Alert.id == alert_id).with_for_update())
    if alert is None:
        raise NotFoundError("Alert not found")
    if alert.status != "resolved":
        now = utc_now()
        alert.status = "resolved"
        if alert.acknowledged_by is None:
            alert.acknowledged_by = actor.id
            alert.acknowledged_at = now
        alert.detail = {**(alert.detail or {}), "resolved_by": str(actor.id)}
        alert.resolved_at = now
        await session.flush()
    return alert
