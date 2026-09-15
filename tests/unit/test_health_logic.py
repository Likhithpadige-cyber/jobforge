"""Readiness aggregation logic.

Verifies the rule from ARCHITECTURE.md §1.3: PostgreSQL down means we cannot
serve; Redis down means we are only degraded.
"""

from __future__ import annotations

import pytest

from apps.api.routers import health as health_router

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_migration_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _revision() -> str:
        return "0001_baseline"

    monkeypatch.setattr(health_router, "get_migration_revision", _revision)


def _stub(monkeypatch: pytest.MonkeyPatch, db: str, redis: str) -> None:
    async def _db() -> dict[str, object]:
        return {"status": db, "latency_ms": 1.0}

    async def _redis() -> dict[str, object]:
        return {"status": redis, "latency_ms": 1.0}

    monkeypatch.setattr(health_router, "check_database", _db)
    monkeypatch.setattr(health_router, "check_redis", _redis)


async def test_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "ok", "ok")
    result = await health_router._build_readiness()
    assert result.status == "ok"
    assert result.migration_revision == "0001_baseline"


async def test_redis_down_is_only_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "ok", "unavailable")
    result = await health_router._build_readiness()
    assert result.status == "degraded"


async def test_postgres_down_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "unavailable", "ok")
    result = await health_router._build_readiness()
    assert result.status == "unavailable"


async def test_both_down_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "unavailable", "unavailable")
    result = await health_router._build_readiness()
    assert result.status == "unavailable"
