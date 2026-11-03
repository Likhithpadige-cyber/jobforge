"""Shared pytest fixtures.

Unit tests run with no external services. Integration tests get real
PostgreSQL and Redis, either from a running compose stack or from
testcontainers, and skip cleanly when neither is available.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LLM_MODE", "replay")
os.environ.setdefault("SUBMISSION_MODE", "dry_run")


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Settings are cached with lru_cache; clear it between tests so that
    monkeypatched environment variables actually take effect."""
    from packages.shared.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    from packages.shared.config import get_settings

    return get_settings()


@pytest.fixture
async def app_client() -> AsyncIterator[object]:
    """ASGI client for the FastAPI app with no live dependencies.

    Startup tolerates unavailable PostgreSQL/Redis by design (see
    apps/api/main.py lifespan), so /health works here and /api/v1/health
    correctly reports the dependencies as unavailable.
    """
    import httpx

    from apps.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _service_available(host: str, port: int, timeout: float = 0.5) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    return _service_available(
        os.getenv("POSTGRES_HOST", "localhost"), int(os.getenv("POSTGRES_PORT", "5432"))
    )


@pytest.fixture(scope="session")
def redis_available() -> bool:
    return _service_available(
        os.getenv("REDIS_HOST", "localhost"), int(os.getenv("REDIS_PORT", "6379"))
    )


@pytest.fixture(autouse=True)
def _no_external_llm_provider(monkeypatch):  # type: ignore[no-untyped-def]
    """Normal tests must use replay or an explicitly injected fake transport."""

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Real external LLM provider calls are forbidden in tests")

    monkeypatch.setattr("packages.agents.llm.runtime.AnthropicTransport.__call__", forbidden)
