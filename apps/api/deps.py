"""Shared FastAPI dependencies.

Authentication follows SECURITY.md §3.1: an opaque server-side session in
Redis, addressed by an HttpOnly cookie, with CSRF double-submit on every
state-changing request.

Everything here fails closed. There is no header, query parameter or request
field anywhere in this file that lets a caller name the user it would like to
be: identity comes from the session record and nothing else. That matters
because `approvals.decided_by` and `applications.approved_by` are written from
this identity, and the database refuses an approval that is not backed by a
real human decision.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.llm.runtime import Prices, StructuredClient
from packages.core.auth import Session, csrf_matches, load_session
from packages.core.cache import get_redis
from packages.core.db import get_session
from packages.core.models import User
from packages.shared.config import Settings, get_settings
from packages.shared.errors import AppError

DbSession = Annotated[AsyncSession, Depends(get_session)]
RedisClient = Annotated[Redis, Depends(get_redis)]
AppSettings = Annotated[Settings, Depends(get_settings)]

#: Reads never need CSRF; every state-changing method does.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The header the frontend echoes the CSRF cookie back in (double submit).
CSRF_HEADER = "X-CSRF-Token"


class NotAuthenticatedError(AppError):
    status_code = 401
    title = "Not Authenticated"
    error_type = "https://jobforge.local/errors/not-authenticated"


class CsrfError(AppError):
    status_code = 403
    title = "CSRF Validation Failed"
    error_type = "https://jobforge.local/errors/csrf"


async def current_session(request: Request, redis: RedisClient, settings: AppSettings) -> Session:
    """The live session for this request, or 401.

    Loading also slides the idle window, so an active user stays signed in and
    an inactive one is logged out by the store rather than by a client-side
    timer.
    """
    session = await load_session(
        redis,
        request.cookies.get(settings.session_cookie_name),
        idle_minutes=settings.session_idle_timeout_minutes,
    )
    if session is None:
        raise NotAuthenticatedError("Sign in to continue")
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


async def current_user(session: CurrentSession, db: DbSession) -> User:
    """The authenticated human behind this request, or 401.

    A session whose user has since been deleted is treated as unauthenticated
    rather than as a server error.
    """
    user = (await db.execute(select(User).where(User.id == session.user_id))).scalar_one_or_none()
    if user is None:
        raise NotAuthenticatedError("Sign in to continue")
    return user


#: The dependency review actions consume. Named for what it guarantees: a real
#: human, established server-side. C2's approve/request-changes endpoints take
#: this and never a reviewer id from the request.
CurrentUser = Annotated[User, Depends(current_user)]
CurrentHumanActor = CurrentUser


async def require_csrf(request: Request, session: CurrentSession) -> Session:
    """Double-submit CSRF check for state-changing requests (SECURITY.md §3.1).

    The token is compared against the copy held in the *server's* session
    record, not merely against another cookie, so setting a cookie from a
    foreign origin is not enough to forge a request.
    """
    if request.method in SAFE_METHODS:
        return session
    if not csrf_matches(session, request.headers.get(CSRF_HEADER)):
        raise CsrfError("Missing or invalid CSRF token")
    return session


#: Attach to any authenticated mutating route.
CsrfProtected = Annotated[Session, Depends(require_csrf)]


def revision_client(settings: AppSettings) -> StructuredClient:
    """Use configured model prices; never guess prices or fall back from replay."""
    prices = None
    if (
        settings.llm_tailoring_input_price_per_million is not None
        and settings.llm_tailoring_output_price_per_million is not None
    ):
        prices = Prices(
            model=settings.llm_model_tailoring,
            input_per_million=settings.llm_tailoring_input_price_per_million,
            output_per_million=settings.llm_tailoring_output_price_per_million,
        )
    return StructuredClient(settings, prices=prices)


RevisionClient = Annotated[StructuredClient, Depends(revision_client)]
