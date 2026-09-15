"""Health endpoints.

Two levels, deliberately separated:

  GET /health          liveness  - no dependency calls, always fast.
                                   Used by Docker/ECS to decide whether to
                                   restart the container.
  GET /api/v1/health   readiness - checks PostgreSQL and Redis.
                                   Used by the load balancer and the dashboard.

Mixing them is a classic outage amplifier: if liveness checked the database,
a brief database blip would restart every container at once.

/healthz and /readyz are aliases kept for consistency with ARCHITECTURE.md §14.3.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Response, status

from apps.api.schemas.health import DependencyHealth, LivenessResponse, ReadinessResponse
from packages.core.cache import check_redis
from packages.core.db import check_database, get_migration_revision
from packages.shared.config import get_settings
from packages.shared.clock import utc_now

SERVICE_VERSION = "0.1.0"
_STARTED_AT = time.monotonic()

router = APIRouter(tags=["health"])
api_router = APIRouter(tags=["health"])


@router.get("/health", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    """Process-level liveness. Touches nothing external."""
    return LivenessResponse(status="ok", service=get_settings().app_name, version=SERVICE_VERSION)


@router.get("/healthz", response_model=LivenessResponse, include_in_schema=False)
async def liveness_alias() -> LivenessResponse:
    return await liveness()


async def _build_readiness() -> ReadinessResponse:
    settings = get_settings()

    db_result, redis_result, revision = await asyncio.gather(
        check_database(),
        check_redis(),
        get_migration_revision(),
    )

    checks = {
        "postgres": DependencyHealth(**db_result),  # type: ignore[arg-type]
        "redis": DependencyHealth(**redis_result),  # type: ignore[arg-type]
    }

    # PostgreSQL is the source of truth: without it we cannot serve.
    # Redis is transport: without it we are slower, not wrong.
    if checks["postgres"].status != "ok":
        overall = "unavailable"
    elif checks["redis"].status != "ok":
        overall = "degraded"
    else:
        overall = "ok"

    return ReadinessResponse(
        status=overall,  # type: ignore[arg-type]
        service=settings.app_name,
        version=SERVICE_VERSION,
        environment=str(settings.environment),
        timestamp=utc_now().isoformat(),
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 2),
        checks=checks,
        migration_revision=revision,
    )


@api_router.get("/health", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(response: Response) -> ReadinessResponse:
    """Dependency-aware readiness.

    Returns 200 for ok/degraded and 503 for unavailable, so an orchestrator
    pulls the instance out of rotation only when it genuinely cannot serve.
    """
    result = await _build_readiness()
    if result.status == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@router.get("/readyz", response_model=ReadinessResponse, include_in_schema=False)
async def readiness_alias(response: Response) -> ReadinessResponse:
    return await readiness(response)
