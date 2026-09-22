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

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.llm.runtime import Prices, StructuredClient
from packages.core.auth import Session, csrf_matches, load_session
from packages.core.cache import Limit, enforce, fingerprint, get_redis
from packages.core.db import get_session
from packages.core.models import User
from packages.services.rendering.store import (
    ArtifactStore,
    ArtifactStoreUnavailableError,
    FilesystemArtifactStore,
    S3ArtifactStore,
)
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


class NotAuthorizedError(AppError):
    """Authenticated, but this role may not do this (M2).

    Deliberately distinct from `NotAuthenticatedError`. 401 means "I do not
    know who you are, sign in"; 403 means "I know exactly who you are and the
    answer is still no". Collapsing them would tell a signed-in reviewer to
    sign in again, and would tell an anonymous caller that the route exists
    for somebody.

    It is also distinct from `CsrfError` despite sharing a status code: the
    `type` field in the problem document differs, so a client - and a test -
    can tell a missing token from an insufficient role.
    """

    status_code = 403
    title = "Not Authorized"
    error_type = "https://jobforge.local/errors/not-authorized"


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


async def authenticated_api(session: CsrfProtected, user: CurrentUser) -> User:
    """The single gate every non-public API route passes through (M1).

    Attached at the router, not the endpoint, and deliberately so. A
    per-endpoint dependency is a list you have to remember to add to, and the
    failure mode of forgetting is silent: the route simply serves anyone. At
    the router the default flips - a new endpoint is protected because of
    where it lives, and making one public is an explicit act someone has to
    write down. `tests/integration/test_authorization_matrix.py` enumerates
    the registered routes and fails if anything is unclassified.

    It composes the two existing dependencies rather than reimplementing
    either. `CsrfProtected` establishes the session and applies the
    double-submit check, which is a no-op on safe methods; `CurrentUser` then
    confirms the session still resolves to a user who exists. FastAPI caches
    `current_session`, so the pair costs one session load, not two.

    The parameter order is load-bearing. Dependencies resolve in declaration
    order, and `CurrentUser` needs a database session while `CsrfProtected`
    needs only Redis and the request. Putting the session check first means an
    anonymous or forged request is refused without ever opening a database
    connection - which is both the cheaper path and the one that still returns
    401 rather than 500 when the database is unreachable.

    Because it runs as a router-level dependency, it resolves before the
    endpoint's own parameters. An anonymous request is refused before any path
    parameter is looked up, so `GET /jobs/{id}` answers 401 whether or not
    that job exists rather than confirming it through a 404.

    This is authentication only. It answers "is there a valid session?" and
    nothing else: an `owner` and a `reviewer` are equivalent here. Role
    authorization is M2 and is deliberately absent - a half-applied role check
    is worse than an honest absence of one.
    """
    return user


#: Router-level gate: a valid session, a user that still exists, and CSRF on
#: every state-changing method.
AuthenticatedApi = Annotated[User, Depends(authenticated_api)]


# --------------------------------------------------------------- M2: roles
#
# `users.role` is a plain text column with a CHECK constraint allowing exactly
# 'owner' and 'reviewer' (packages/core/models/identity.py). No schema change
# was needed for M2 and none was made: the two roles the product defines are
# already the two the database permits, and widening that set is a migration
# and a policy decision, not a convenience.

OWNER = "owner"
REVIEWER = "reviewer"

#: Every role the system recognises. A value outside this set cannot reach the
#: database, but the dependency still fails closed if one ever does.
KNOWN_ROLES = frozenset({OWNER, REVIEWER})


def require_role(*allowed: str):  # type: ignore[no-untyped-def]
    """Build a dependency that admits only the named roles.

    Composed on top of `authenticated_api` rather than beside it, so the whole
    M1 chain - session, user existence, CSRF - runs first and unchanged. This
    dependency adds one comparison and nothing else; there is no second place
    where a session is loaded or a token is checked.

    Attached per endpoint, not per router. Owner-only routes are the minority
    and they are the sensitive ones, so naming each of them at its decorator
    makes the privileged surface greppable: `grep -rn require_owner` lists
    every mutation a reviewer cannot perform. The route matrix test then
    checks that list against the registered routes, so neither can drift
    without the other noticing.

    Because it is a dependency, it resolves before the endpoint's own
    parameters. A reviewer calling an owner-only route is refused before the
    path parameter is looked up, so a forbidden id and a nonexistent one are
    indistinguishable - 403 either way, never a 404 that confirms the row.
    """
    permitted = frozenset(allowed)

    async def dependency(user: AuthenticatedApi) -> User:
        if user.role not in permitted:
            raise NotAuthorizedError("This action requires a different role")
        return user

    return dependency


#: The owner-only gate. Read the policy off the decorators, not off a table.
require_owner = require_role(OWNER)

#: For an endpoint that wants the user object as well as the check.
RequireOwner = Annotated[User, Depends(require_owner)]


# ---------------------------------------------------------- M3: rate limits
#
# The limiter itself lives in packages/core/cache/rate_limit.py. What belongs
# here is only the wiring: how a principal is identified, and how a limit is
# attached to a route.


def source_fingerprint(request: Request, settings: Settings) -> str:
    """A keyed digest of the peer address, for unauthenticated limiting.

    `request.client.host` is the socket peer and nothing else. No forwarded
    header is read, because this application has no trusted-proxy model: if it
    honoured `X-Forwarded-For`, any caller could set that header to a fresh
    value per request and the per-address budget would be worth nothing.

    The consequence is worth stating plainly rather than discovering in
    production. Deployed behind a proxy that does not preserve the peer
    address, every request appears to come from the proxy, and this limit
    becomes a single global budget for login instead of a per-client one. That
    is a real reduction in precision - it is not a bypass, and it fails in the
    safe direction - but it is the reason a trusted-proxy model belongs on the
    list before this goes behind CloudFront.
    """
    host = request.client.host if request.client else "unknown"
    return fingerprint(f"ip:{host}", settings.session_secret)


def identifier_fingerprint(email: str, settings: Settings) -> str:
    """A keyed digest of a *submitted* login identifier.

    Submitted, not resolved. The caller hashes whatever arrived in the request
    body without looking it up first, so an address that has no account
    consumes exactly the same budget, in exactly the same way, as one that
    does. That property is what keeps this limiter from becoming an account
    oracle, and it is the reason this function takes a string rather than a
    `User`.

    The address is normalised the way the login query treats it - trimmed and
    lowercased - so that `A@B.com` and `a@b.com ` share one budget instead of
    two.
    """
    return fingerprint(f"email:{email.strip().lower()}", settings.session_secret)


def login_ip_limit(settings: Settings) -> Limit:
    return Limit(
        name="login-ip",
        max_requests=settings.rate_limit_login_per_ip,
        window_seconds=settings.rate_limit_login_ip_window_seconds,
    )


def login_identifier_limit(settings: Settings) -> Limit:
    return Limit(
        name="login-id",
        max_requests=settings.rate_limit_login_per_identifier,
        window_seconds=settings.rate_limit_login_identifier_window_seconds,
    )


def revision_limit(settings: Settings) -> Limit:
    return Limit(
        name="revision",
        max_requests=settings.rate_limit_revision_per_user,
        window_seconds=settings.rate_limit_revision_window_seconds,
    )


def import_limit(settings: Settings) -> Limit:
    return Limit(
        name="job-import",
        max_requests=settings.rate_limit_import_per_user,
        window_seconds=settings.rate_limit_import_window_seconds,
    )


def cover_letter_revision_limit(settings: Settings) -> Limit:
    return Limit(
        name="cover-letter-revision",
        max_requests=settings.rate_limit_cover_letter_revision_per_user,
        window_seconds=settings.rate_limit_cover_letter_revision_window_seconds,
    )


def fact_suggest_limit(settings: Settings) -> Limit:
    return Limit(
        name="fact-suggest",
        max_requests=settings.rate_limit_fact_suggest_per_user,
        window_seconds=settings.rate_limit_fact_suggest_window_seconds,
    )


def rate_limited(build: Callable[[Settings], Limit]):  # type: ignore[no-untyped-def]
    """Build a dependency that charges one request to the signed-in user.

    Scoped to the user id, so two principals never share a budget and one
    noisy reviewer cannot exhaust the owner's. It depends on
    `AuthenticatedApi`, which means the M1 chain runs first: an anonymous
    caller is refused 401 without touching a counter, so an attacker cannot
    spend somebody else's budget without first holding their session.

    Where a route is also owner-only, list `require_owner` before this one in
    the decorator. A reviewer then gets 403 without consuming the owner's
    quota, which is the difference between a wrong role and a spent budget.
    """

    async def dependency(
        user: AuthenticatedApi,
        redis: RedisClient,
        settings: AppSettings,
    ) -> None:
        if not settings.rate_limit_enabled:
            return
        await enforce(redis, build(settings), str(user.id))

    return dependency


def artifact_download_limit(settings: Settings) -> Limit:
    return Limit(
        name="artifact-download",
        max_requests=settings.rate_limit_artifact_download_per_user,
        window_seconds=settings.rate_limit_artifact_download_window_seconds,
    )


def artifact_store(settings: AppSettings) -> ArtifactStore:
    """The configured object store, or a refusal.

    There is no "fall back to the filesystem if S3 is not configured" branch,
    and that absence is the point. A deployment that believes it is writing to
    private S3 must not quietly start writing resumes onto a container's local
    disk, where they survive no restart, appear in no backup, and are governed
    by none of the bucket's access rules.
    """
    if settings.artifact_storage_backend == "filesystem":
        return FilesystemArtifactStore(settings.artifact_storage_root)
    if settings.artifact_storage_backend == "s3":
        return S3ArtifactStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise ArtifactStoreUnavailableError(
        f"unknown artifact storage backend {settings.artifact_storage_backend!r}"
    )


ArtifactStoreDep = Annotated[ArtifactStore, Depends(artifact_store)]


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
