"""Shared type aliases and small value objects."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

type EntityId = UUID
type ActorType = Literal["agent", "human", "system"]
type AggregateType = Literal["job", "application", "resume", "approval", "submission", "system"]
type HealthState = Literal["ok", "degraded", "unavailable"]
