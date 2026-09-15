"""Response models for the health endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    """GET /health - is this process alive?"""

    status: Literal["ok"] = "ok"
    service: str = Field(examples=["jobforge"])
    version: str = Field(examples=["0.1.0"])


class DependencyHealth(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    latency_ms: float | None = None
    error: str | None = None


class ReadinessResponse(BaseModel):
    """GET /api/v1/health - are we ready to serve real traffic?"""

    status: Literal["ok", "degraded", "unavailable"]
    service: str
    version: str
    environment: str
    timestamp: str
    uptime_seconds: float
    checks: dict[str, DependencyHealth]
    migration_revision: str | None = None
