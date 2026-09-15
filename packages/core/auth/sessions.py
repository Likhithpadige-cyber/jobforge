"""Server-side sessions in Redis (SECURITY.md §3.1).

Sessions are server-side so they can be revoked: a cookie alone cannot be
withdrawn once issued, but a Redis key can be deleted, and the next request
then fails closed. The cookie therefore carries an opaque random id and
nothing else - no user id, no role, no profile data, nothing an attacker could
read or tamper with offline.

Two independent expiries, both enforced on every read:

* **idle**, 12 hours by default, refreshed on each authenticated request;
* **absolute**, 7 days, never refreshed - a session dies a week after login
  no matter how actively it is used.

The Redis TTL is set to whichever bound expires sooner, so an abandoned
session is reaped by Redis itself rather than lingering until someone reads
it. The absolute deadline is *also* checked in Python, because a TTL is a
liveness optimisation and the deadline is the actual rule.

The CSRF token is stored in the session record and compared server-side, which
is what makes the double-submit pattern meaningful: an attacker who can guess
or set a cookie still cannot produce a header matching the *server's* copy.
"""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from packages.shared.clock import utc_now

#: Redis key prefix. Kept distinct so a flush of session keys cannot touch
#: cached application data.
KEY_PREFIX = "session:"

#: 256 bits of entropy in the cookie and in the CSRF token.
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class Session:
    """A live session as stored in Redis. Never contains secrets."""

    id: str
    user_id: UUID
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime

    def as_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "user_id": str(self.user_id),
                "csrf_token": self.csrf_token,
                "created_at": self.created_at.isoformat(),
                "last_seen_at": self.last_seen_at.isoformat(),
                "absolute_expires_at": self.absolute_expires_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Session | None:
        """Parse a stored record, returning None for anything unreadable."""
        try:
            data = json.loads(raw)
            return cls(
                id=str(data["id"]),
                user_id=UUID(str(data["user_id"])),
                csrf_token=str(data["csrf_token"]),
                created_at=datetime.fromisoformat(data["created_at"]),
                last_seen_at=datetime.fromisoformat(data["last_seen_at"]),
                absolute_expires_at=datetime.fromisoformat(data["absolute_expires_at"]),
            )
        except (ValueError, TypeError, KeyError):
            return None


def _key(session_id: str) -> str:
    return f"{KEY_PREFIX}{session_id}"


def _aware(value: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC rather than crashing on compare."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ttl_seconds(session: Session, idle_minutes: int, now: datetime) -> int:
    """Seconds until whichever bound - idle or absolute - expires first."""
    idle_deadline = now + timedelta(minutes=idle_minutes)
    deadline = min(idle_deadline, _aware(session.absolute_expires_at))
    return max(int((deadline - now).total_seconds()), 1)


async def create_session(
    redis,
    *,
    user_id: UUID,
    idle_minutes: int,
    absolute_hours: int,
) -> Session:
    """Issue a new session and its CSRF token."""
    now = utc_now()
    session = Session(
        id=secrets.token_urlsafe(_TOKEN_BYTES),
        user_id=user_id,
        csrf_token=secrets.token_urlsafe(_TOKEN_BYTES),
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + timedelta(hours=absolute_hours),
    )
    await redis.set(
        _key(session.id),
        session.as_json(),
        ex=_ttl_seconds(session, idle_minutes, now),
    )
    return session


async def load_session(
    redis,
    session_id: str | None,
    *,
    idle_minutes: int,
) -> Session | None:
    """Load a live session, sliding the idle window, or return None.

    Returns None - never raises - for a missing, unparseable, idle-expired or
    absolutely-expired session, so every caller fails closed the same way.
    An expired record is deleted on the way out rather than left to rot.
    """
    if not session_id:
        return None
    raw = await redis.get(_key(session_id))
    if raw is None:
        return None
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8", errors="replace")
    session = Session.from_json(raw)
    if session is None:
        await redis.delete(_key(session_id))
        return None

    now = utc_now()
    if now >= _aware(session.absolute_expires_at):
        await redis.delete(_key(session_id))
        return None
    if now - _aware(session.last_seen_at) >= timedelta(minutes=idle_minutes):
        # Idle expiry. Redis would usually have reaped this already; the
        # explicit check covers a record whose TTL was extended or restored.
        await redis.delete(_key(session_id))
        return None

    refreshed = Session(
        id=session.id,
        user_id=session.user_id,
        csrf_token=session.csrf_token,
        created_at=session.created_at,
        last_seen_at=now,
        absolute_expires_at=session.absolute_expires_at,
    )
    await redis.set(
        _key(refreshed.id),
        refreshed.as_json(),
        ex=_ttl_seconds(refreshed, idle_minutes, now),
    )
    return refreshed


async def revoke_session(redis, session_id: str | None) -> bool:
    """Delete a session. True when one was actually removed."""
    if not session_id:
        return False
    return bool(await redis.delete(_key(session_id)))


def csrf_matches(session: Session, presented: str | None) -> bool:
    """Compare the presented CSRF token against the server's copy.

    `compare_digest` so the comparison does not leak the token through timing.
    """
    if not presented:
        return False
    return hmac.compare_digest(session.csrf_token, presented)
