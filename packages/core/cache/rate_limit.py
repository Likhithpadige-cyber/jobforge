"""Redis-backed fixed-window rate limiting (SECURITY.md §3, M3).

Why a fixed window, and not a sliding window or a token bucket
--------------------------------------------------------------
A fixed window is two commands in one transaction, and every property this
system needs falls out of it:

* **Atomic.** `INCR` is atomic per key, so N concurrent requests receive N
  distinct counts. There is no read-modify-write for a race to slip through,
  which is the failure mode that makes naive limiters trivially bypassable.
* **Always expires.** `PEXPIRE ... NX` sets the TTL exactly once, when the
  window opens, and never extends it. That matters more here than accuracy:
  a counter that could lose its TTL - which is what `INCR` followed by a
  separate `EXPIRE` risks if the process dies in between - would become a
  permanent block. On the login path that is an account lockout created by
  accident, and lockouts are explicitly not wanted.
* **Cheap.** One round trip, two integers per principal, no per-request
  member sets to trim.

The cost is the boundary case: up to `limit` requests may land at the end of
one window and `limit` more at the start of the next, so the true worst case
is 2x over a window-length interval. At these limits - single digits per
quarter hour - that is not a meaningful weakening, and it is not worth the
memory and complexity of a sorted-set sliding window to remove.

Identity is hashed, never stored
--------------------------------
Keys carry an HMAC of the principal, not the principal. Redis never holds an
email address or an IP in the clear, and the HMAC is keyed with the session
secret so the hashes are not reversible by dictionary attack across the small
space of IPv4 addresses. Nothing about a credential - not the password, not
the code, not their lengths - reaches this module at all.
"""

from __future__ import annotations

import hmac
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from packages.shared.errors import AppError, DependencyUnavailableError
from packages.shared.logging import get_logger

log = get_logger(__name__)

#: Every key this module writes starts here, so an operator can see, count
#: and drop the whole limiter namespace without touching sessions.
KEY_PREFIX = "rl"


class CounterPipeline(Protocol):
    """Queued commands used by the fixed-window transaction."""

    def incr(self, key: str, /) -> object: ...

    def pexpire(self, key: str, ms: int, /, *, nx: bool) -> object: ...

    def pttl(self, key: str, /) -> object: ...

    async def execute(self) -> list[int]: ...


class RateLimitStore(Protocol):
    def pipeline(self, *, transaction: bool) -> AbstractAsyncContextManager[CounterPipeline]: ...


class RateLimitedError(AppError):
    """429, with `Retry-After` both as a header and in the problem body.

    The header is what a well-behaved client obeys; the body field is what a
    human reading the response sees. Emitting only one of the two is how a
    retry loop ends up guessing.
    """

    status_code = 429
    title = "Too Many Requests"
    error_type = "https://jobforge.local/errors/rate-limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Too many requests. Try again later.",
            retry_after=retry_after,
        )
        self.headers = {"Retry-After": str(retry_after)}


@dataclass(frozen=True)
class Limit:
    """One named budget: how many, over how long.

    `name` becomes part of the key, so two limits never share a counter even
    when they are scoped to the same principal.
    """

    name: str
    max_requests: int
    window_seconds: int


@dataclass(frozen=True)
class Decision:
    """What the limiter concluded. Returned for logging and for tests."""

    allowed: bool
    count: int
    limit: int
    retry_after: int


def fingerprint(value: str, secret: str) -> str:
    """Keyed, truncated digest of a principal.

    Truncation to 32 hex characters (128 bits) keeps keys short; collision
    resistance at that width is far beyond what a counter namespace needs.
    """
    mac = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), sha256)
    return mac.hexdigest()[:32]


def key_for(limit: Limit, identity: str) -> str:
    return f"{KEY_PREFIX}:{limit.name}:{limit.window_seconds}:{identity}"


async def consume(redis: RateLimitStore, limit: Limit, identity: str) -> Decision:
    """Count one request against `identity` and say whether it may proceed.

    The counter is incremented whether or not the request is allowed. That is
    deliberate on the login path: an attacker who keeps hammering a blocked
    key keeps it blocked, and a caller cannot learn where the threshold sits
    by watching for the first refusal to stop counting.

    Raises `DependencyUnavailableError` when Redis cannot be reached. Every
    endpoint this module protects already requires Redis - login writes a
    session, and the authenticated endpoints read one - so failing closed adds
    no new outage mode, and it removes the alternative: an attacker who can
    disrupt Redis would otherwise switch the brute-force protection off.
    """
    key = key_for(limit, identity)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.pexpire(key, limit.window_seconds * 1000, nx=True)
            pipe.pttl(key)
            count, _, pttl = await pipe.execute()
    except (RateLimitedError, DependencyUnavailableError):
        raise
    except Exception as exc:
        log.error("rate_limit.backend_unavailable", limit=limit.name, error=type(exc).__name__)
        raise DependencyUnavailableError(
            "Rate limiting is unavailable; refusing the request"
        ) from None

    count = int(count)
    # A missing or negative PTTL means the key has no TTL yet as far as this
    # read is concerned. Falling back to the full window is the safe rounding:
    # it never advertises a retry time earlier than the window can honour.
    remaining_ms = int(pttl) if pttl is not None and int(pttl) > 0 else limit.window_seconds * 1000
    retry_after = max(1, -(-remaining_ms // 1000))  # ceil, never 0

    return Decision(
        allowed=count <= limit.max_requests,
        count=count,
        limit=limit.max_requests,
        retry_after=retry_after,
    )


async def enforce(redis: RateLimitStore, limit: Limit, identity: str) -> Decision:
    """`consume`, but raise 429 when the budget is spent."""
    decision = await consume(redis, limit, identity)
    if not decision.allowed:
        log.warning("rate_limit.exceeded", limit=limit.name, count=decision.count)
        raise RateLimitedError(decision.retry_after)
    return decision
