"""The fixed-window limiter, proven without Redis and without sleeping.

Every timing assertion here moves a fake clock rather than waiting on a real
one. A test that sleeps to watch a window expire is slow, flaky on a loaded
machine, and proves less: it cannot show what happens at the instant the key
expires, only some time afterwards.
"""

from __future__ import annotations

import asyncio

import pytest

from packages.core.cache.rate_limit import (
    KEY_PREFIX,
    Limit,
    RateLimitedError,
    consume,
    enforce,
    fingerprint,
    key_for,
)
from packages.shared.errors import DependencyUnavailableError
from tests.unit.test_auth_sessions import FakeRedis

pytestmark = pytest.mark.unit

SECRET = "unit-test-secret-not-a-real-key"
LIMIT = Limit(name="probe", max_requests=3, window_seconds=60)


# Compatibility name for existing fixtures; all suites now use the same store.
FakeLimiterRedis = FakeRedis


@pytest.fixture
def redis() -> FakeLimiterRedis:
    return FakeLimiterRedis()


# ------------------------------------------------------------- the budget
async def test_requests_below_the_threshold_are_allowed(redis) -> None:
    for expected in (1, 2, 3):
        decision = await consume(redis, LIMIT, "alice")
        assert decision.allowed is True
        assert decision.count == expected


async def test_the_request_past_the_threshold_is_refused(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    with pytest.raises(RateLimitedError):
        await enforce(redis, LIMIT, "alice")


async def test_refusals_keep_counting(redis) -> None:
    """A blocked caller who keeps hammering stays blocked.

    If the counter stopped at the limit, a caller could find the threshold by
    watching for the count to stop moving - and, worse, a burst arriving
    together could each read the same stalled value.
    """
    for _ in range(LIMIT.max_requests):
        await consume(redis, LIMIT, "alice")
    first = await consume(redis, LIMIT, "alice")
    second = await consume(redis, LIMIT, "alice")
    assert (first.allowed, second.allowed) == (False, False)
    assert second.count == first.count + 1


# ------------------------------------------------------------------ 429
async def test_the_error_carries_retry_after_in_header_and_body(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    with pytest.raises(RateLimitedError) as caught:
        await enforce(redis, LIMIT, "alice")

    error = caught.value
    assert error.status_code == 429
    assert error.headers["Retry-After"] == str(error.extra["retry_after"])

    problem = error.to_problem(instance="/api/v1/auth/login")
    assert problem["status"] == 429
    assert problem["type"] == "https://jobforge.local/errors/rate-limited"
    assert problem["retry_after"] == error.extra["retry_after"]


async def test_retry_after_tracks_the_remaining_window(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    redis.advance(45)
    with pytest.raises(RateLimitedError) as caught:
        await enforce(redis, LIMIT, "alice")
    # 15s left of a 60s window, rounded up and never zero.
    assert caught.value.extra["retry_after"] == 15


async def test_retry_after_is_never_zero(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    redis.advance(59.9)
    with pytest.raises(RateLimitedError) as caught:
        await enforce(redis, LIMIT, "alice")
    assert caught.value.extra["retry_after"] >= 1


# ---------------------------------------------------------------- windows
async def test_the_window_recovers_when_the_key_expires(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    with pytest.raises(RateLimitedError):
        await enforce(redis, LIMIT, "alice")

    redis.advance(LIMIT.window_seconds + 1)

    decision = await consume(redis, LIMIT, "alice")
    assert decision.allowed is True
    assert decision.count == 1, "the counter did not reset with the window"


async def test_the_window_is_fixed_and_is_not_extended_by_traffic(redis) -> None:
    """The TTL is set once. Continued traffic must not push it out.

    An extending TTL turns a window into an indefinite block, which is the
    lockout this design is specifically avoiding.
    """
    await consume(redis, LIMIT, "alice")
    key = key_for(LIMIT, "alice")
    first_deadline = redis.expires_at[key]

    redis.advance(30)
    await consume(redis, LIMIT, "alice")
    assert redis.expires_at[key] == first_deadline


async def test_an_expired_key_starts_a_genuinely_new_window(redis) -> None:
    await consume(redis, LIMIT, "alice")
    redis.advance(LIMIT.window_seconds + 1)
    await consume(redis, LIMIT, "alice")
    key = key_for(LIMIT, "alice")
    assert redis.expires_at[key] == pytest.approx(redis.now + LIMIT.window_seconds)


# ----------------------------------------------------------- concurrency
async def test_concurrent_callers_cannot_exceed_the_limit(redis) -> None:
    """Twenty simultaneous requests against a budget of three.

    Exactly three are allowed, with distinct counts from atomic transactions.
    """
    results = await asyncio.gather(*(consume(redis, LIMIT, "alice") for _ in range(20)))
    assert sum(1 for r in results if r.allowed) == LIMIT.max_requests
    assert sorted(r.count for r in results) == list(range(1, 21))


# ------------------------------------------------------------- isolation
async def test_two_principals_do_not_share_a_budget(redis) -> None:
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    with pytest.raises(RateLimitedError):
        await enforce(redis, LIMIT, "alice")

    decision = await consume(redis, LIMIT, "bob")
    assert decision.allowed is True and decision.count == 1


async def test_two_limits_do_not_share_a_budget(redis) -> None:
    """The same principal under two named limits has two counters."""
    other = Limit(name="other-probe", max_requests=3, window_seconds=60)
    for _ in range(LIMIT.max_requests):
        await enforce(redis, LIMIT, "alice")
    assert (await consume(redis, other, "alice")).allowed is True


# ------------------------------------------------------------------ keys
def test_keys_are_namespaced_and_carry_the_window() -> None:
    key = key_for(LIMIT, "abc123")
    assert key.startswith(f"{KEY_PREFIX}:")
    assert key == "rl:probe:60:abc123"


def test_the_fingerprint_hides_the_principal() -> None:
    """No email and no address reaches Redis in the clear."""
    digest = fingerprint("email:person@example.invalid", SECRET)
    assert "person@example.invalid" not in digest
    assert "@" not in digest
    assert len(digest) == 32
    assert digest == fingerprint("email:person@example.invalid", SECRET)


def test_the_fingerprint_is_keyed() -> None:
    """A different secret gives a different digest.

    Without the key, the whole IPv4 space could be enumerated against a leaked
    key listing in minutes.
    """
    assert fingerprint("ip:203.0.113.4", SECRET) != fingerprint("ip:203.0.113.4", "other")


# ---------------------------------------------------- backend unavailable
async def test_an_unreachable_backend_fails_closed(redis) -> None:
    """503, not "allowed".

    Failing open would mean an attacker who can disrupt Redis also switches
    off brute-force protection. Every endpoint behind this limiter already
    needs Redis - login writes a session, the rest read one - so refusing here
    adds no outage that was not already there.
    """
    redis.fail = True
    with pytest.raises(DependencyUnavailableError) as caught:
        await consume(redis, LIMIT, "alice")
    assert caught.value.status_code == 503
    assert not isinstance(caught.value, RateLimitedError)


async def test_the_backend_error_does_not_leak_transport_detail(redis) -> None:
    redis.fail = True
    with pytest.raises(DependencyUnavailableError) as caught:
        await consume(redis, LIMIT, "alice")
    assert "redis is down" not in str(caught.value)
