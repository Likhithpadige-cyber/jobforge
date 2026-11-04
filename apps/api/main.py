"""FastAPI application entrypoint.

The API process is deliberately thin: it validates, reads and writes state,
and streams events. It never calls an LLM synchronously (ARCHITECTURE.md §1.2).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.routers import (
    agent_runs,
    alerts,
    applications,
    artifacts,
    auth,
    cover_letter_review,
    facts,
    first_draft,
    health,
    jobs,
    monitoring,
    profile,
    resume_review,
    resumes,
    review_status,
)
from apps.api.routers import (
    settings as settings_router,
)
from packages.core.cache import close_redis, init_redis
from packages.core.db import (
    dispose_audit_engine,
    dispose_engine,
    init_audit_engine,
    init_engine,
)
from packages.shared.config import get_settings
from packages.shared.errors import AppError
from packages.shared.ids import uuid7_str
from packages.shared.logging import configure_logging, get_logger

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared connections at startup, release them at shutdown.

    Connection failures are logged but not fatal: the API still starts and
    reports itself unhealthy through /api/v1/health, which is far easier to
    diagnose than a container that crash-loops before it can serve a request.
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log.info(
        "api.starting",
        environment=str(settings.environment),
        submission_mode=str(settings.submission_mode),
        llm_mode=str(settings.llm_mode),
    )

    try:
        await init_engine(settings)
    except Exception as exc:
        log.error("api.startup.database_failed", error=type(exc).__name__)

    # Separate engine, separate pool, separate failure. A domain database that
    # is up while the audit one is not is a real state, and the API reports it
    # rather than refusing to start.
    #
    # To be clear about what that does and does not buy today: in this batch a
    # failed audit engine means the orchestration diagnostics are lost, and
    # nothing more. It does NOT stop a model call - `AuditBoundary.record()`
    # exists and raises `AuditUnavailableError` for a caller that wants to
    # gate on evidence, but no caller does yet. Wiring the inference stages to
    # refuse to start without a landed audit write is Batch 3B's job, and
    # until then this startup path has no fail-closed behaviour to defer to.
    try:
        await init_audit_engine(settings)
    except Exception as exc:
        log.error("api.startup.audit_database_failed", error=type(exc).__name__)

    try:
        await init_redis(settings)
    except Exception as exc:
        log.error("api.startup.redis_failed", error=type(exc).__name__)

    log.info("api.started", port=settings.api_port)
    try:
        yield
    finally:
        log.info("api.stopping")
        await close_redis()
        await dispose_audit_engine()
        await dispose_engine()
        log.info("api.stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Jobforge API",
        description=(
            "AI Job Application Platform. Human-in-the-loop by default; "
            "see docs/SECURITY.md for the safety model."
        ),
        version="0.1.0",
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a request id, bind log context, and time the request."""
        request_id = request.headers.get("X-Request-ID") or uuid7_str()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.unhandled")
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        if request.url.path not in ("/health", "/healthz", "/metrics"):
            log.info(
                "request.completed",
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        return response

    register_exception_handlers(app)

    app.include_router(alerts.router, prefix=settings.api_prefix)
    app.include_router(monitoring.router, prefix=settings.api_prefix)
    app.include_router(review_status.router, prefix=settings.api_prefix)
    app.include_router(agent_runs.router, prefix=settings.api_prefix)
    app.include_router(health.router)
    app.include_router(health.api_router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(profile.router, prefix=settings.api_prefix)
    app.include_router(facts.router, prefix=settings.api_prefix)
    app.include_router(jobs.router, prefix=settings.api_prefix)
    app.include_router(jobs.sources_router, prefix=settings.api_prefix)
    app.include_router(resumes.router, prefix=settings.api_prefix)
    app.include_router(resumes.applications_router, prefix=settings.api_prefix)
    app.include_router(resume_review.router, prefix=settings.api_prefix)
    app.include_router(first_draft.router, prefix=settings.api_prefix)
    app.include_router(artifacts.router, prefix=settings.api_prefix)
    app.include_router(applications.router, prefix=settings.api_prefix)
    app.include_router(cover_letter_review.router, prefix=settings.api_prefix)
    app.include_router(settings_router.router, prefix=settings.api_prefix)

    return app


def register_exception_handlers(app: FastAPI) -> None:
    """RFC 7807 problem details for every error path (ARCHITECTURE.md §5.7)."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning("request.app_error", error=type(exc).__name__, status=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_problem(instance=str(request.url.path)),
            media_type=PROBLEM_JSON,
            # Empty for every error but 429, which carries Retry-After.
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "https://jobforge.local/errors/validation",
                "title": "Validation Failed",
                "status": 422,
                "detail": "The request body or parameters failed validation.",
                "instance": str(request.url.path),
                "errors": [
                    {"loc": list(err.get("loc", [])), "msg": err.get("msg", "")}
                    for err in exc.errors()
                ],
            },
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "about:blank",
                "title": str(exc.detail),
                "status": exc.status_code,
                "detail": str(exc.detail),
                "instance": str(request.url.path),
            },
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a stack trace to the client (SECURITY.md §8.1).
        log.exception("request.internal_error", error=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "type": "about:blank",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "An unexpected error occurred.",
                "instance": str(request.url.path),
            },
            media_type=PROBLEM_JSON,
        )


app = create_app()
