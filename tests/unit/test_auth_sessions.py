"""Session lifecycle, expiry, revocation and CSRF comparison.

Redis is faked with a small in-memory double rather than mocked call-by-call,
so the tests exercise the real store/read/expire logic instead of asserting
that particular Redis commands were issued.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from packages.core.auth import (
    KEY_PREFIX,
    Session,
    create_session,
    csrf_matches,
    load_session,
    revoke_session,
)
from packages.shared.clock import utc_now

pytestmark = [pytest.mark.unit, pytest.mark.safety]

USER = UUID(int=7)
IDLE = 720  # 12 hours
ABSOLUTE = 168  # 7 days


class FakeRedis:
    """Shared string store with a controlled clock and atomic Redis transactions."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expires_at: dict[str, float] = {}
        self.now = 1_000.0
        self.fail = False

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for key in list(self.expires_at):
            self._expire(key)

    def _expire(self, key: str) -> None:
        if self.expires_at.get(key, float("inf")) <= self.now:
            self.store.pop(key, None)
            self.counters.pop(key, None)
            self.ttls.pop(key, None)
            self.expires_at.pop(key, None)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.counters.pop(key, None)
        self.ttls.pop(key, None)
        self.expires_at.pop(key, None)
        if ex is not None:
            self.ttls[key] = ex
            self.expires_at[key] = self.now + ex

    async def get(self, key: str) -> str | None:
        self._expire(key)
        return str(self.counters[key]) if key in self.counters else self.store.get(key)

    async def delete(self, key: str) -> int:
        self._expire(key)
        self.ttls.pop(key, None)
        self.expires_at.pop(key, None)
        existed = key in self.store or key in self.counters
        self.store.pop(key, None)
        self.counters.pop(key, None)
        return int(existed)

    def _incr(self, key: str) -> int:
        self._expire(key)
        value = int(self.counters.get(key, self.store.get(key, "0"))) + 1
        self.store.pop(key, None)
        self.counters[key] = value
        return value

    def _pexpire(self, key: str, ms: int, nx: bool) -> int:
        self._expire(key)
        if (key not in self.store and key not in self.counters) or (nx and key in self.expires_at):
            return 0
        self.expires_at[key] = self.now + ms / 1000
        self._expire(key)
        return 1

    def _pttl(self, key: str) -> int:
        self._expire(key)
        if key not in self.store and key not in self.counters:
            return -2
        if key not in self.expires_at:
            return -1
        return int(round((self.expires_at[key] - self.now) * 1000))

    def pipeline(self, transaction: bool = True):  # type: ignore[no-untyped-def]
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        self.ops.clear()
        return False

    def incr(self, key: str):  # type: ignore[no-untyped-def]
        self.ops.append(("incr", key))
        return self

    def pexpire(self, key: str, ms: int, nx: bool = False):  # type: ignore[no-untyped-def]
        self.ops.append(("pexpire", key, ms, nx))
        return self

    def pttl(self, key: str):  # type: ignore[no-untyped-def]
        self.ops.append(("pttl", key))
        return self

    async def execute(self) -> list:
        if self.redis.fail:
            raise ConnectionError("redis is down")
        # No await between commands: MULTI/EXEC cannot interleave with another
        # client's commands. Clear the queue just as redis-py does after EXEC.
        ops, self.ops = self.ops, []
        return [getattr(self.redis, "_" + op[0])(*op[1:]) for op in ops]


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


async def new_session(redis: FakeRedis) -> Session:
    return await create_session(redis, user_id=USER, idle_minutes=IDLE, absolute_hours=ABSOLUTE)


def store_at(redis: FakeRedis, session: Session, replacement: Session) -> None:
    redis.store[f"{KEY_PREFIX}{session.id}"] = replacement.as_json()


# --- creation ---------------------------------------------------------------


async def test_session_is_stored_in_redis(redis: FakeRedis) -> None:
    session = await new_session(redis)
    assert f"{KEY_PREFIX}{session.id}" in redis.store
    assert session.user_id == USER


async def test_session_id_and_csrf_are_distinct_high_entropy_tokens(
    redis: FakeRedis,
) -> None:
    session = await new_session(redis)
    assert session.id != session.csrf_token
    assert len(session.id) >= 32 and len(session.csrf_token) >= 32


async def test_two_sessions_never_collide(redis: FakeRedis) -> None:
    first, second = await new_session(redis), await new_session(redis)
    assert first.id != second.id
    assert first.csrf_token != second.csrf_token


async def test_stored_record_carries_no_credentials(redis: FakeRedis) -> None:
    session = await new_session(redis)
    raw = redis.store[f"{KEY_PREFIX}{session.id}"]
    for forbidden in ("password", "totp", "secret_enc"):
        assert forbidden not in raw.lower()


async def test_ttl_never_exceeds_the_absolute_deadline(redis: FakeRedis) -> None:
    """A 1-hour absolute limit must cap a 12-hour idle window."""
    session = await create_session(redis, user_id=USER, idle_minutes=IDLE, absolute_hours=1)
    assert redis.ttls[f"{KEY_PREFIX}{session.id}"] <= 3600


# --- loading and expiry -----------------------------------------------------


async def test_valid_session_loads(redis: FakeRedis) -> None:
    session = await new_session(redis)
    loaded = await load_session(redis, session.id, idle_minutes=IDLE)
    assert loaded is not None and loaded.id == session.id


async def test_loading_slides_the_idle_window(redis: FakeRedis) -> None:
    session = await new_session(redis)
    stale = Session(
        id=session.id,
        user_id=session.user_id,
        csrf_token=session.csrf_token,
        created_at=session.created_at,
        last_seen_at=utc_now() - timedelta(minutes=30),
        absolute_expires_at=session.absolute_expires_at,
    )
    store_at(redis, session, stale)
    loaded = await load_session(redis, session.id, idle_minutes=IDLE)
    assert loaded is not None
    assert loaded.last_seen_at > stale.last_seen_at


async def test_idle_expiry_is_enforced_and_reaped(redis: FakeRedis) -> None:
    session = await new_session(redis)
    idle = Session(
        id=session.id,
        user_id=session.user_id,
        csrf_token=session.csrf_token,
        created_at=session.created_at,
        last_seen_at=utc_now() - timedelta(minutes=IDLE + 1),
        absolute_expires_at=session.absolute_expires_at,
    )
    store_at(redis, session, idle)
    assert await load_session(redis, session.id, idle_minutes=IDLE) is None
    assert f"{KEY_PREFIX}{session.id}" not in redis.store


async def test_absolute_expiry_beats_recent_activity(redis: FakeRedis) -> None:
    """Active use cannot extend a session past seven days."""
    session = await new_session(redis)
    aged = Session(
        id=session.id,
        user_id=session.user_id,
        csrf_token=session.csrf_token,
        created_at=utc_now() - timedelta(hours=ABSOLUTE + 1),
        last_seen_at=utc_now(),  # busy right now
        absolute_expires_at=utc_now() - timedelta(seconds=1),
    )
    store_at(redis, session, aged)
    assert await load_session(redis, session.id, idle_minutes=IDLE) is None


@pytest.mark.parametrize("session_id", [None, "", "never-issued"])
async def test_unknown_session_returns_none(redis: FakeRedis, session_id: str | None) -> None:
    assert await load_session(redis, session_id, idle_minutes=IDLE) is None


async def test_unparseable_record_is_discarded(redis: FakeRedis) -> None:
    session = await new_session(redis)
    redis.store[f"{KEY_PREFIX}{session.id}"] = "{not json"
    assert await load_session(redis, session.id, idle_minutes=IDLE) is None
    assert f"{KEY_PREFIX}{session.id}" not in redis.store


# --- revocation -------------------------------------------------------------


async def test_revoked_session_is_rejected(redis: FakeRedis) -> None:
    session = await new_session(redis)
    assert await revoke_session(redis, session.id) is True
    assert await load_session(redis, session.id, idle_minutes=IDLE) is None


async def test_revoking_twice_reports_nothing_removed(redis: FakeRedis) -> None:
    session = await new_session(redis)
    await revoke_session(redis, session.id)
    assert await revoke_session(redis, session.id) is False


async def test_revoking_nothing_is_safe(redis: FakeRedis) -> None:
    assert await revoke_session(redis, None) is False


# --- CSRF -------------------------------------------------------------------


async def test_csrf_matches_the_servers_copy(redis: FakeRedis) -> None:
    session = await new_session(redis)
    assert csrf_matches(session, session.csrf_token)


@pytest.mark.parametrize("presented", [None, "", "wrong-token"])
async def test_csrf_rejects_anything_else(redis: FakeRedis, presented: str | None) -> None:
    session = await new_session(redis)
    assert not csrf_matches(session, presented)


async def test_csrf_token_of_another_session_is_rejected(redis: FakeRedis) -> None:
    """Double submit binds to *this* session, not to any valid-looking token."""
    first, second = await new_session(redis), await new_session(redis)
    assert not csrf_matches(first, second.csrf_token)
