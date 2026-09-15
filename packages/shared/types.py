"""Shared type aliases and small value objects."""

from __future__ import annotations

from typing import Literal, TypeAlias
from uuid import UUID

EntityId: TypeAlias = UUID
ActorType: TypeAlias = Literal["agent", "human", "system"]
AggregateType: TypeAlias = Literal[
    "job", "application", "resume", "approval", "submission", "system"
]
HealthState: TypeAlias = Literal["ok", "degraded", "unavailable"]
