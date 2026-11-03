"""Login, logout, /me, cookie flags and CSRF, against a real database.

The endpoint functions are called directly with the test session, as the other
API integration tests do. Redis is faked so the suite does not depend on a
live instance for what is really session-record logic.
"""

from __future__ import annotations

import pytest
from fastapi import Request, Response
from sqlalchemy import select

from apps.api.deps import CsrfError, NotAuthenticatedError, current_user, require_csrf
from apps.api.routers.auth import auth_config, login, logout, me
from apps.api.schemas.auth import LoginRequest
from packages.core.auth import (
    create_session,
    csrf_matches,
    generate_secret,
    hash_password,
    load_session,
)
from packages.core.crypto import encrypt_totp_secret, generate_encryption_key
from packages.core.models import User
from packages.shared.config import Settings
from tests.unit.test_auth_sessions import FakeRedis

pytestmark = pytest.mark.integration

PASSWORD = "demo-password-not-real"


#: Synthetic, non-functional stand-ins. `Settings` refuses to build for
#: staging/production with these empty (`_require_secrets_outside_local`), and
#: that guard is deliberate - so the tests satisfy it rather than relax it.
#: Same shape as the dummies already used in tests/unit/test_config.py.
TEST_SECRETS = {
    "postgres_password": "test-postgres-password",
    "session_secret": "test-session-secret",
    "anthropic_api_key": "test-anthropic-key",
}


def settings_for(environment: str = "test", **overrides: object) -> Settings:
    """Settings for one environment, valid in every environment.

    The secrets are supplied unconditionally rather than only for
    staging/production: `Settings` ignores them locally, and a helper that
    behaves the same everywhere is one less thing to reason about.
    """
    return Settings(
        environment=environment,
        totp_encryption_key=generate_encryption_key(),
        session_idle_timeout_minutes=720,
        session_absolute_timeout_hours=168,
        **{**TEST_SECRETS, **overrides},
    )


def code_for(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).now()


@pytest.fixture
async def enrolled(session, demo_user):  # type: ignore[no-untyped-def]
    """The demo user with a password and an enrolled TOTP secret."""
    cfg = settings_for()
    secret = generate_secret()
    user = (await session.execute(select(User).where(User.id == demo_user))).scalar_one()
    user.password_hash = hash_password(PASSWORD)
    user.totp_secret_enc = encrypt_totp_secret(secret, cfg.totp_encryption_key)
    await session.flush()
    return user, secret, cfg


def request_with(headers: dict[str, str] | None = None, method: str = "POST") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "headers": raw, "path": "/"})


# --- login ------------------------------------------------------------------


async def test_login_with_both_factors_succeeds(session, enrolled) -> None:
    user, secret, cfg = enrolled
    redis, response = FakeRedis(), Response()
    result = await login(
        LoginRequest(email=user.email, password=PASSWORD, totp_code=code_for(secret)),
        request_with(),
        response,
        session,
        redis,
        cfg,
    )
    assert result.user.email == user.email
    assert result.csrf_token
    assert len(redis.store) == 1  # a session was stored server-side


async def test_login_sets_httponly_strict_session_cookie(session, enrolled) -> None:
    user, secret, cfg = enrolled
    response = Response()
    await login(
        LoginRequest(email=user.email, password=PASSWORD, totp_code=code_for(secret)),
        request_with(),
        response,
        session,
        FakeRedis(),
        cfg,
    )
    cookies = response.headers.getlist("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(cfg.session_cookie_name))
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie.replace("Strict", "strict")
    # The CSRF cookie must be readable by the frontend - that is the point.
    csrf_cookie = next(c for c in cookies if c.startswith(cfg.csrf_cookie_name))
    assert "HttpOnly" not in csrf_cookie


async def test_secure_flag_follows_the_environment(session, enrolled) -> None:
    user, secret, _cfg = enrolled
    local = settings_for("local")
    local.totp_encryption_key = _cfg.totp_encryption_key
    prod = settings_for("production")
    prod.totp_encryption_key = _cfg.totp_encryption_key

    for cfg, expect_secure in ((local, False), (prod, True)):
        response = Response()
        await login(
            LoginRequest(email=user.email, password=PASSWORD, totp_code=code_for(secret)),
            request_with(),
            response,
            session,
            FakeRedis(),
            cfg,
        )
        cookie = next(
            c
            for c in response.headers.getlist("set-cookie")
            if c.startswith(cfg.session_cookie_name)
        )
        assert ("Secure" in cookie) is expect_secure


@pytest.mark.parametrize("field", ["password", "totp_code"])
async def test_a_wrong_factor_is_always_rejected(session, enrolled, field: str) -> None:
    """A wrong password, or a wrong code, fails in every environment.

    Note the second case: `test` does not *demand* a code, but a code that is
    supplied is still verified, so a wrong one still fails.
    """
    user, secret, cfg = enrolled
    payload = {
        "email": user.email,
        "password": PASSWORD,
        "totp_code": code_for(secret),
    }
    payload[field] = "000000" if field == "totp_code" else "wrong-password"
    with pytest.raises(NotAuthenticatedError):
        await login(LoginRequest(**payload), request_with(), Response(), session, FakeRedis(), cfg)


async def test_unknown_email_is_rejected_identically(session, enrolled) -> None:
    _user, secret, cfg = enrolled
    with pytest.raises(NotAuthenticatedError) as exc:
        await login(
            LoginRequest(
                email="nobody@example.com",
                password=PASSWORD,
                totp_code=code_for(secret),
            ),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            cfg,
        )
    assert "Invalid email, password or code" in str(exc.value)


async def test_user_without_credentials_cannot_sign_in(session, demo_user) -> None:
    """A seeded user who never ran the setup command has no way in."""
    cfg = settings_for()
    user = (await session.execute(select(User).where(User.id == demo_user))).scalar_one()
    assert user.password_hash is None
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password=PASSWORD, totp_code="123456"),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            cfg,
        )


async def test_login_never_returns_secrets(session, enrolled) -> None:
    user, secret, cfg = enrolled
    result = await login(
        LoginRequest(email=user.email, password=PASSWORD, totp_code=code_for(secret)),
        request_with(),
        Response(),
        session,
        FakeRedis(),
        cfg,
    )
    body = result.model_dump_json()
    assert "password_hash" not in body
    assert "totp_secret" not in body
    assert secret not in body


# --- MFA policy by environment (SECURITY.md §3.6) ---------------------------


async def test_local_login_succeeds_without_a_code(session, enrolled) -> None:
    """Local development: email + password is enough."""
    user, _secret, cfg = enrolled
    local = settings_for("local")
    local.totp_encryption_key = cfg.totp_encryption_key
    redis = FakeRedis()

    result = await login(
        LoginRequest(email=user.email, password=PASSWORD),
        request_with(),
        Response(),
        session,
        redis,
        local,
    )
    assert result.user.email == user.email
    assert len(redis.store) == 1  # a real server-side session, as always


async def test_local_login_still_rejects_a_wrong_password(session, enrolled) -> None:
    """Relaxing the second factor does not relax the first."""
    user, _secret, cfg = enrolled
    local = settings_for("local")
    local.totp_encryption_key = cfg.totp_encryption_key
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password="wrong-password"),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            local,
        )


async def test_local_still_verifies_a_code_that_is_supplied(session, enrolled) -> None:
    """Optional is not ignored: a wrong code offered locally still fails."""
    user, _secret, cfg = enrolled
    local = settings_for("local")
    local.totp_encryption_key = cfg.totp_encryption_key
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password=PASSWORD, totp_code="000000"),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            local,
        )


async def test_local_session_and_csrf_are_unchanged(session, enrolled) -> None:
    """The relaxation touches the second factor only, nothing downstream."""
    user, _secret, cfg = enrolled
    local = settings_for("local")
    local.totp_encryption_key = cfg.totp_encryption_key
    redis, response = FakeRedis(), Response()

    result = await login(
        LoginRequest(email=user.email, password=PASSWORD),
        request_with(),
        response,
        session,
        redis,
        local,
    )
    cookie = next(
        c for c in response.headers.getlist("set-cookie") if c.startswith(local.session_cookie_name)
    )
    assert "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()
    assert result.csrf_token

    # The cookie's opaque id must address a real server-side session whose
    # CSRF token matches the one handed to the client.
    session_id = cookie.split("=", 1)[1].split(";", 1)[0]
    live = await load_session(redis, session_id, idle_minutes=local.session_idle_timeout_minutes)
    assert live is not None
    assert live.user_id == user.id
    assert csrf_matches(live, result.csrf_token)


async def test_production_requires_a_code(session, enrolled) -> None:
    """Outside local/test the second factor is mandatory."""
    user, _secret, cfg = enrolled
    prod = settings_for("production")
    prod.totp_encryption_key = cfg.totp_encryption_key
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password=PASSWORD),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            prod,
        )


async def test_production_accepts_a_valid_code(session, enrolled) -> None:
    user, secret, cfg = enrolled
    prod = settings_for("production")
    prod.totp_encryption_key = cfg.totp_encryption_key
    result = await login(
        LoginRequest(email=user.email, password=PASSWORD, totp_code=code_for(secret)),
        request_with(),
        Response(),
        session,
        FakeRedis(),
        prod,
    )
    assert result.user.email == user.email


async def test_production_rejects_an_invalid_code(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    prod = settings_for("production")
    prod.totp_encryption_key = cfg.totp_encryption_key
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password=PASSWORD, totp_code="000000"),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            prod,
        )


async def test_production_rejects_an_unenrolled_user(session, demo_user) -> None:
    """No stored secret means no way to satisfy a mandatory second factor."""
    prod = settings_for("production")
    user = (await session.execute(select(User).where(User.id == demo_user))).scalar_one()
    user.password_hash = hash_password(PASSWORD)
    await session.flush()
    with pytest.raises(NotAuthenticatedError):
        await login(
            LoginRequest(email=user.email, password=PASSWORD, totp_code="000000"),
            request_with(),
            Response(),
            session,
            FakeRedis(),
            prod,
        )


@pytest.mark.parametrize(
    ("environment", "required"),
    [("local", False), ("test", False), ("staging", True), ("production", True)],
)
async def test_auth_config_reports_the_policy(environment: str, required: bool) -> None:
    cfg = settings_for(environment)
    result = await auth_config(cfg)
    assert result.totp_required is required
    # One boolean and nothing else - no user data, no secret.
    assert set(result.model_dump()) == {"totp_required"}


async def test_production_settings_still_fail_closed_without_secrets() -> None:
    """The helper satisfies the secrets guard; it does not disable it.

    If this ever stops raising, `Settings` has been weakened and a production
    deployment could boot with empty secrets - which is exactly what
    `_require_secrets_outside_local` exists to prevent.
    """
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError) as exc:
        Settings(
            environment="production",
            totp_encryption_key=generate_encryption_key(),
        )
    assert "Missing required secrets" in str(exc.value)


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_helper_supplies_only_synthetic_secrets(environment: str) -> None:
    """Nothing real ever reaches a test fixture."""
    cfg = settings_for(environment)
    assert cfg.session_secret == "test-session-secret"
    assert cfg.anthropic_api_key.startswith("test-")
    assert not cfg.is_local


# --- /me and the current-user dependency ------------------------------------


async def test_me_requires_a_valid_session(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    redis = FakeRedis()
    live = await create_session(
        redis,
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    resolved = await current_user(live, session)
    assert resolved.id == user.id
    assert (await me(resolved)).email == user.email


async def test_me_omits_every_secret(session, enrolled) -> None:
    user, _secret, _cfg = enrolled
    body = (await me(user)).model_dump_json()
    assert "password_hash" not in body and "totp_secret_enc" not in body


async def test_session_for_a_deleted_user_is_not_authenticated(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    redis = FakeRedis()
    live = await create_session(
        redis,
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    await session.delete(user)
    await session.flush()
    with pytest.raises(NotAuthenticatedError):
        await current_user(live, session)


# --- CSRF -------------------------------------------------------------------


async def test_csrf_required_for_a_mutating_request(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    live = await create_session(
        FakeRedis(),
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    with pytest.raises(CsrfError):
        await require_csrf(request_with(), live)


async def test_wrong_csrf_token_is_rejected(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    live = await create_session(
        FakeRedis(),
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    with pytest.raises(CsrfError):
        await require_csrf(request_with({"X-CSRF-Token": "forged"}), live)


async def test_valid_csrf_token_is_accepted(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    live = await create_session(
        FakeRedis(),
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    assert (await require_csrf(request_with({"X-CSRF-Token": live.csrf_token}), live)).id == live.id


async def test_reads_do_not_require_csrf(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    live = await create_session(
        FakeRedis(),
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    assert (await require_csrf(request_with(method="GET"), live)).id == live.id


# --- logout -----------------------------------------------------------------


async def test_logout_revokes_the_session_and_clears_cookies(session, enrolled) -> None:
    user, _secret, cfg = enrolled
    redis = FakeRedis()
    live = await create_session(
        redis,
        user_id=user.id,
        idle_minutes=cfg.session_idle_timeout_minutes,
        absolute_hours=cfg.session_absolute_timeout_hours,
    )
    response = Response()
    result = await logout(request_with(), response, redis, cfg, live)

    assert result.revoked is True
    assert redis.store == {}  # gone server-side, not merely un-cookied
    cleared = response.headers.getlist("set-cookie")
    assert any(cfg.session_cookie_name in c for c in cleared)
    assert any(cfg.csrf_cookie_name in c for c in cleared)
