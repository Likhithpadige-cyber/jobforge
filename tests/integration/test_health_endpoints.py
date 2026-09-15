"""Health endpoint tests against the real ASGI app.

These run without PostgreSQL or Redis: startup tolerates missing
dependencies, so /health returns 200 and /api/v1/health honestly reports
what is unavailable. When the compose stack is up, the same tests assert
the healthy path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_liveness_returns_ok(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "jobforge"
    assert body["version"]


async def test_liveness_does_not_depend_on_services(app_client) -> None:  # type: ignore[no-untyped-def]
    """Liveness must never touch PostgreSQL or Redis - otherwise a database
    blip restarts every container at once."""
    response = await app_client.get("/health")
    assert response.status_code == 200
    assert "checks" not in response.json()


async def test_healthz_alias(app_client) -> None:  # type: ignore[no-untyped-def]
    assert (await app_client.get("/healthz")).status_code == 200


async def test_readiness_reports_dependency_detail(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/api/v1/health")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"]) == {"postgres", "redis"}
    assert body["status"] in ("ok", "degraded", "unavailable")
    assert body["environment"] == "test"
    assert body["uptime_seconds"] >= 0


async def test_readiness_status_code_matches_status(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/api/v1/health")
    body = response.json()
    if body["status"] == "unavailable":
        assert response.status_code == 503
    else:
        assert response.status_code == 200


async def test_request_id_header_is_returned(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/health")
    assert response.headers.get("X-Request-ID")


async def test_supplied_request_id_is_echoed(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


async def test_unknown_route_returns_problem_details(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert {"type", "title", "status", "detail"} <= set(body)


async def test_no_stack_trace_is_ever_leaked(app_client) -> None:  # type: ignore[no-untyped-def]
    response = await app_client.get("/api/v1/does-not-exist")
    assert "Traceback" not in response.text
