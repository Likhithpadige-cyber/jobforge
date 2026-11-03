"""Rate limiting over HTTP (M3).

Marked `safety`: a regression here is a security regression, not a
performance one. If login stops being limited, the only remaining brake on a
credential-stuffing run is Argon2's cost factor.

No test in this file sleeps. The limiter's clock is the fake Redis clock, and
window recovery is asserted by moving it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.deps import CSRF_HEADER
from apps.api.schemas.auth import LoginRequest
from packages.core.cache import redis as redis_module
from packages.shared.config import get_settings
from packages.shared.ids import uuid7
from tests.unit.test_rate_limit import FakeLimiterRedis

pytestmark = [pytest.mark.integration, pytest.mark.safety]

RATE_LIMIT_TYPE = "https://jobforge.local/errors/rate-limited"
CSRF_TYPE = "https://jobforge.local/errors/csrf"
ROLE_TYPE = "https://jobforge.local/errors/not-authorized"

KNOWN_PASSWORD = "not-the-real-password"

#: `LoginRequest.email` is `EmailStr`, and email-validator rejects the
#: special-use domains - .invalid, .test, .local, .localhost - at the syntax
#: level, with no DNS lookup involved. A login body addressed to one of them
#: never reaches the handler, so it never reaches the limiter either: it is a
#: 422 from request validation. The rest of the suite can use .invalid freely
#: because those addresses go into the database as SQL literals; only the ones
#: that travel through the login schema are constrained.
EMAIL_DOMAIN = "example.com"


def login_body(email: str) -> dict:
    """A login payload built by the production schema, not hand-rolled.

    Constructing `LoginRequest` here means a test cannot send a body the API
    would reject: an invalid address fails at construction, in this file, with
    the real validation error - rather than arriving as a mystery 422 that
    looks like a limiter defect. That is exactly the failure this helper
    exists to prevent, and it keeps the schema defined in one place.
    """
    return LoginRequest(email=email, password=KNOWN_PASSWORD).model_dump(
        exclude_none=True, mode="json"
    )


@pytest.fixture
def small_limits(monkeypatch):  # type: ignore[no-untyped-def]
    """Tighten every budget so a test can exhaust one in a few calls.

    Set through the environment rather than by patching a module constant, so
    the code under test reads them exactly as a deployment would.
    """
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_IP", "6")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_IDENTIFIER", "3")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP_WINDOW_SECONDS", "900")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IDENTIFIER_WINDOW_SECONDS", "900")
    monkeypatch.setenv("RATE_LIMIT_REVISION_PER_USER", "2")
    monkeypatch.setenv("RATE_LIMIT_REVISION_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("RATE_LIMIT_IMPORT_PER_USER", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def limiter_redis(app_client, engine):  # type: ignore[no-untyped-def]
    """Install a clock-controlled store for the unauthenticated tests."""
    previous = redis_module._client
    fake = FakeLimiterRedis()
    redis_module._client = fake  # type: ignore[assignment]
    try:
        yield fake
    finally:
        redis_module._client = previous


async def _login(client, email: str):  # type: ignore[no-untyped-def]
    return await client.post("/api/v1/auth/login", json=login_body(email))


@pytest.fixture
async def registered_email(engine):  # type: ignore[no-untyped-def]
    """A user that really exists, with no usable password.

    The password is deliberately unset: every login in this file must fail on
    credentials, so the only thing varying between the "known" and "unknown"
    cases is whether the row exists.
    """
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    user_id = uuid7()
    email = f"known-{uuid4().hex[:12]}@{EMAIL_DOMAIN}"
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as setup:
        await setup.execute(
            text(
                "INSERT INTO users (id, email, role, created_at, updated_at) "
                "VALUES (:id, :email, 'owner', now(), now())"
            ),
            {"id": user_id, "email": email},
        )
        await setup.commit()
    try:
        yield email
    finally:
        async with maker() as teardown:
            await teardown.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await teardown.commit()


# ===================================================================== login
async def test_attempts_below_the_threshold_are_not_limited(
    app_client, limiter_redis, small_limits
) -> None:
    email = f"a-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(3):
        response = await _login(app_client, email)
        assert response.status_code == 401, response.text


async def test_the_attempt_past_the_threshold_is_refused_with_429(
    app_client, limiter_redis, small_limits
) -> None:
    email = f"b-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(3):
        assert (await _login(app_client, email)).status_code == 401

    refused = await _login(app_client, email)
    assert refused.status_code == 429
    body = refused.json()
    assert body["type"] == RATE_LIMIT_TYPE
    assert body["status"] == 429
    assert body["retry_after"] >= 1
    assert refused.headers["Retry-After"] == str(body["retry_after"])


async def test_the_window_recovers(app_client, limiter_redis, small_limits) -> None:
    email = f"c-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(3):
        await _login(app_client, email)
    assert (await _login(app_client, email)).status_code == 429

    limiter_redis.advance(901)

    assert (await _login(app_client, email)).status_code == 401


async def test_rotating_the_email_still_meets_the_address_budget(
    app_client, limiter_redis, small_limits
) -> None:
    """Per-identifier limiting alone would be bypassed by a fresh address
    each time. The per-source budget is what closes that."""
    seen = set()
    for _ in range(6):
        response = await _login(app_client, f"rot-{uuid7()}@{EMAIL_DOMAIN}")
        seen.add(response.status_code)
    assert seen == {401}, seen

    refused = await _login(app_client, f"rot-{uuid7()}@{EMAIL_DOMAIN}")
    assert refused.status_code == 429
    assert refused.json()["type"] == RATE_LIMIT_TYPE


async def test_limiting_does_not_reveal_whether_an_email_exists(
    app_client, limiter_redis, small_limits, registered_email
) -> None:
    """The oracle test.

    One address has an account, the other does not. Both are driven past the
    per-identifier budget and every observable - status code, problem type,
    the presence and shape of Retry-After - must match at every step. If the
    limiter looked the address up before counting, or skipped counting for an
    unknown address, these sequences would diverge.
    """

    async def trace(email: str) -> list[tuple]:
        out = []
        for _ in range(4):
            response = await _login(app_client, email)
            body = response.json()
            out.append(
                (
                    response.status_code,
                    body.get("type"),
                    "Retry-After" in response.headers,
                )
            )
        return out

    unknown = await trace(f"unknown-{uuid7()}@{EMAIL_DOMAIN}")
    # A fresh store, so the second address starts from the same budget.
    limiter_redis.advance(901)
    known = await trace(registered_email)

    assert known == unknown, f"known={known} unknown={unknown}"
    assert known[-1][0] == 429


async def test_a_refusal_does_not_mint_a_session(app_client, limiter_redis, small_limits) -> None:
    """429 must not be a side door: no cookie, no session record."""
    email = f"d-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(3):
        await _login(app_client, email)
    refused = await _login(app_client, email)

    assert refused.status_code == 429
    settings = get_settings()
    assert settings.session_cookie_name not in refused.cookies
    assert limiter_redis.store == {}


async def test_a_malformed_body_is_rejected_before_the_limiter_and_costs_nothing(
    app_client, limiter_redis, small_limits
) -> None:
    """Documents the real ordering, rather than assuming it.

    On this endpoint the limiter runs *inside* the handler, because the
    per-identifier budget needs the parsed email. FastAPI validates the body
    before it calls the handler, so a body that fails validation returns 422
    and never reaches a counter.

    Stated plainly so nobody has to rediscover it: **a malformed login body
    consumes no rate-limit quota.** That is not a hole in credential
    protection - a request that cannot be parsed cannot guess a password -
    but it does mean this limiter is not a flood control for garbage traffic,
    which belongs at the edge.

    An empty body, a missing password and an address the schema refuses all
    take the same path.
    """
    refusals = [
        {},
        {"email": f"x-{uuid7()}@{EMAIL_DOMAIN}"},
        {"email": "not-an-address", "password": KNOWN_PASSWORD},
        {"email": f"y-{uuid7()}@example.invalid", "password": KNOWN_PASSWORD},
    ]
    for body in refusals:
        response = await app_client.post("/api/v1/auth/login", json=body)
        assert response.status_code == 422, (body, response.status_code)

    assert limiter_redis.counters == {}, "a rejected body charged a budget"

    # And the budget really is untouched: a well-formed attempt still gets its
    # full allowance afterwards.
    email = f"z-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(3):
        assert (await _login(app_client, email)).status_code == 401


def test_the_login_schema_refuses_special_use_domains() -> None:
    """Pins the root cause of the first M3 integration failure.

    Not a limiter property - a schema one. `EmailStr` rejects .invalid at the
    syntax level, which is why the original tests saw 422 where they expected
    401. Kept so the next person who reaches for a .invalid address in a login
    payload is told why in one line instead of debugging the limiter.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        LoginRequest(email="someone@example.invalid", password=KNOWN_PASSWORD)

    assert LoginRequest(email=f"someone@{EMAIL_DOMAIN}", password=KNOWN_PASSWORD).email.endswith(
        EMAIL_DOMAIN
    )


# ================================================ authenticated expensive work
def _revision_path(application_id) -> str:  # type: ignore[no-untyped-def]
    return f"/api/v1/applications/{application_id}/resume-review/request-changes"


async def test_request_changes_is_limited_per_user(api_sessions, small_limits) -> None:
    """The budget is spent before the application is even looked up.

    A random id would normally answer 404. Once the budget is gone the answer
    becomes 429 instead, which is the proof that the limiter runs ahead of the
    handler - and therefore that a caller cannot spend provider budget by
    guessing ids.
    """
    owner = await api_sessions("owner")
    body = {"approval_id": str(uuid7()), "note": "please revise the summary"}

    for _ in range(2):
        response = await owner.post(_revision_path(uuid7()), json=body)
        assert response.status_code != 429, response.text

    refused = await owner.post(_revision_path(uuid7()), json=body)
    assert refused.status_code == 429
    assert refused.json()["type"] == RATE_LIMIT_TYPE
    assert "Retry-After" in refused.headers


async def test_one_users_revision_budget_is_not_anothers(api_sessions, small_limits) -> None:
    owner = await api_sessions("owner")
    reviewer = await api_sessions("reviewer")
    body = {"approval_id": str(uuid7()), "note": "please revise the summary"}

    for _ in range(3):
        await owner.post(_revision_path(uuid7()), json=body)
    assert (await owner.post(_revision_path(uuid7()), json=body)).status_code == 429

    other = await reviewer.post(_revision_path(uuid7()), json=body)
    assert other.status_code != 429, "budgets are shared between principals"


def test_the_rate_limit_does_not_replace_the_rework_rule() -> None:
    """Two different controls, and the limiter is the looser of the two.

    The three-cycle rework rule bounds one application and is enforced in the
    service against committed approvals; this bounds one user per hour and is
    enforced in Redis. Configuring the rate limit below the rework limit would
    let an operator quietly weaken the workflow rule by tuning an operational
    knob, so the default must stay at or above it.

    The 409 behaviour itself is owned by
    tests/integration/test_resume_rework_api.py and is untouched by M3.

    Deliberately reads the defaults, with no `small_limits` override: the
    invariant is about what ships, not about what a test configured.
    """
    settings = get_settings()
    assert settings.rate_limit_revision_per_user >= settings.revision_max_attempts


# ============================================== M1 / M2 / CSRF still in force
async def test_anonymous_callers_are_refused_before_any_budget(
    app_client, limiter_redis, small_limits
) -> None:
    """401 comes first. A caller with no session cannot spend anyone's quota."""
    for _ in range(5):
        response = await app_client.post("/api/v1/jobs/import", json={})
        assert response.status_code == 401
    assert limiter_redis.counters == {}


async def test_a_reviewer_is_refused_by_role_before_any_budget(api_sessions, small_limits) -> None:
    """403 for the wrong role, and the owner's import budget is untouched."""
    reviewer = await api_sessions("reviewer")
    for _ in range(5):
        response = await reviewer.post("/api/v1/jobs/import", json={})
        assert response.status_code == 403
        assert response.json()["type"] == ROLE_TYPE

    owner = await api_sessions("owner")
    allowed = await owner.post("/api/v1/jobs/import", json={})
    assert allowed.status_code != 429, "a refused reviewer spent the owner's budget"


async def test_csrf_is_still_enforced_ahead_of_the_limiter(api_sessions, small_limits) -> None:
    """A session without a token is refused for CSRF, not for rate limiting,
    and the missing token does not consume the budget either."""
    owner = await api_sessions("owner")
    owner.headers.pop(CSRF_HEADER, None)
    for _ in range(5):
        response = await owner.post("/api/v1/jobs/import", json={})
        assert response.status_code == 403
        assert response.json()["type"] == CSRF_TYPE


async def test_rate_limiting_can_be_disabled_without_touching_auth(
    app_client, limiter_redis, small_limits, monkeypatch
) -> None:
    """The switch turns off counting and nothing else.

    It exists for deployments that limit at the edge. It must never become a
    way to reach an endpoint that authentication would otherwise refuse.
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    email = f"e-{uuid7()}@{EMAIL_DOMAIN}"
    for _ in range(8):
        assert (await _login(app_client, email)).status_code == 401
    assert limiter_redis.counters == {}

    assert (await app_client.post("/api/v1/jobs/import", json={})).status_code == 401
