"""Authentication endpoints (SECURITY.md §3.1).

Password (argon2id) and TOTP, then an opaque server-side session in Redis
addressed by an HttpOnly cookie.

TOTP is mandatory in every environment except `local` and `test`, where it is
optional so a developer is not reaching for a phone on every reload
(SECURITY.md §3.6). The relaxation is narrow and server-side: the storage,
crypto and verification code are identical either way, a code supplied locally
is still verified, and no request field or header can influence the decision.

Two deliberate choices worth stating:

*Login failures are indistinguishable.* An unknown email, a wrong password, a
user with no credentials set and a wrong TOTP code all return the same 401
with the same message, and the password is verified even when the user does
not exist, so response timing does not reveal which accounts are real.

*Login is the one mutating route without CSRF.* There is no session to bind a
token to yet, which is the standard exception; the cookie it sets is
`SameSite=Strict`, so it cannot be replayed cross-site afterwards.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from apps.api.deps import (
    AppSettings,
    CsrfProtected,
    CurrentUser,
    DbSession,
    NotAuthenticatedError,
    RedisClient,
)
from apps.api.schemas.auth import (
    AuthConfigRead,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    UserRead,
)
from packages.core.auth import (
    create_session,
    hash_password,
    revoke_session,
    verify_code,
    verify_password,
)
from packages.core.crypto import DecryptionError, decrypt_totp_secret
from packages.core.models import User
from packages.shared.clock import utc_now
from packages.shared.config import Settings
from packages.shared.logging import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

#: Burned to equalise timing when the account does not exist. Never a real
#: credential - it only exists so `verify_password` does comparable work.
_DUMMY_HASH_INPUT = "not-a-real-password"


def _totp_ok(user: User, code: str, settings: Settings) -> bool:
    """Decrypt the enrolled secret and verify the code. False on any problem.

    An undecryptable secret is logged as an operational error - it means the
    key changed or the row is corrupt - but reported to the caller as an
    ordinary failed login.
    """
    if not user.totp_secret_enc:
        return False
    try:
        secret = decrypt_totp_secret(user.totp_secret_enc, settings.totp_encryption_key)
    except DecryptionError:
        log.error("auth.login.totp_undecryptable", user_id=str(user.id))
        return False
    return verify_code(secret, code)


def _cookie_kwargs(settings: Settings) -> dict[str, Any]:
    """Cookie flags per SECURITY.md §3.1.

    `Secure` is driven by the environment: local and test run over plain HTTP,
    where a Secure cookie would simply never be sent and login would appear to
    succeed while leaving the browser unauthenticated. Everywhere else it is
    on. `HttpOnly` and `SameSite=Strict` are unconditional.
    """
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": not settings.is_local,
        "path": "/",
    }


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    kwargs = _cookie_kwargs(settings)
    response.delete_cookie(settings.session_cookie_name, path=kwargs["path"])
    response.delete_cookie(settings.csrf_cookie_name, path=kwargs["path"])


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Sign in with password and TOTP",
)
async def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
    redis: RedisClient,
    settings: AppSettings,
) -> Any:
    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    # Verify a hash either way, so a missing account and a wrong password take
    # comparable time and cannot be told apart by an attacker enumerating emails.
    stored_hash = user.password_hash if user else None
    password_ok = verify_password(payload.password, stored_hash)
    if user is None or not password_ok:
        if user is None:
            verify_password(payload.password, hash_password(_DUMMY_HASH_INPUT))
        log.info("auth.login.failed", reason="credentials")
        raise NotAuthenticatedError("Invalid email, password or code")

    # Second factor. Mandatory everywhere except local/test (SECURITY.md §3.6);
    # the decision comes from settings, never from the request.
    if settings.totp_required:
        if not user.totp_secret_enc or not payload.totp_code:
            # A missing code and an unenrolled account are both refusals, and
            # both look like every other failure from outside.
            log.info("auth.login.failed", reason="totp_missing")
            raise NotAuthenticatedError("Invalid email, password or code")
        if not _totp_ok(user, payload.totp_code, settings):
            log.info("auth.login.failed", reason="totp")
            raise NotAuthenticatedError("Invalid email, password or code")
    elif payload.totp_code and user.totp_secret_enc:
        # Local development does not demand a code, but a code that *is*
        # supplied is still checked. Accepting a wrong one because the
        # environment is relaxed would teach the wrong thing and hide a real
        # enrolment problem until production.
        if not _totp_ok(user, payload.totp_code, settings):
            log.info("auth.login.failed", reason="totp")
            raise NotAuthenticatedError("Invalid email, password or code")

    session = await create_session(
        redis,
        user_id=user.id,
        idle_minutes=settings.session_idle_timeout_minutes,
        absolute_hours=settings.session_absolute_timeout_hours,
    )
    kwargs = _cookie_kwargs(settings)
    response.set_cookie(settings.session_cookie_name, session.id, **kwargs)
    # The CSRF cookie is readable by the frontend on purpose - that is the
    # "double" in double-submit - so it alone is not HttpOnly.
    response.set_cookie(
        settings.csrf_cookie_name,
        session.csrf_token,
        **{**kwargs, "httponly": False},
    )

    user.last_login_at = utc_now()
    await db.commit()
    log.info("auth.login.succeeded", user_id=str(user.id))
    return LoginResponse(
        user=UserRead.model_validate(user), csrf_token=session.csrf_token
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Revoke the current session",
)
async def logout(
    request: Request,
    response: Response,
    redis: RedisClient,
    settings: AppSettings,
    session: CsrfProtected,
) -> Any:
    """Revoke server-side, then clear the cookies.

    Revocation is what makes this real: deleting the cookie alone would leave
    a working session id in anyone's hands who had copied it.
    """
    revoked = await revoke_session(redis, session.id)
    _clear_session_cookies(response, settings)
    log.info("auth.logout", revoked=revoked)
    return LogoutResponse(revoked=revoked)


@router.get(
    "/config",
    response_model=AuthConfigRead,
    summary="Which credentials this deployment requires",
)
async def auth_config(settings: AppSettings) -> Any:
    """Unauthenticated by necessity: the sign-in form reads it before login.

    Returns one boolean. It reveals only the deployment's MFA policy, which is
    already observable by attempting a login, and no user or secret data.
    """
    return AuthConfigRead(totp_required=settings.totp_required)


@router.get(
    "/me",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    summary="The currently authenticated user",
)
async def me(user: CurrentUser) -> Any:
    """Session bootstrap for the frontend. 401 when not signed in."""
    return UserRead.model_validate(user)
