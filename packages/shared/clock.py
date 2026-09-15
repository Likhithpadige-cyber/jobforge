"""Canonical time handling.

Every timestamp in this system is timezone-aware UTC. That is not a style
preference: the database stores TIMESTAMPTZ, so a naive datetime either
raises on comparison or - worse - is silently interpreted in the server's
local zone and stored an hour or several off.

Use `utc_now()` rather than `datetime.now(UTC)` directly. Two reasons:

* one place to change if the project ever needs an injectable clock for
  deterministic tests, and
* a single symbol to grep for, which makes the ruff DTZ rule's job easy to
  verify by eye.

Never use `datetime.utcnow()`. It returns a NAIVE datetime despite the name,
which is the single most common source of this bug class in Python.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def utc_now() -> datetime:
    """The current instant, timezone-aware, in UTC."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Today's date in UTC. Use for quota buckets and review-due comparisons."""
    return datetime.now(UTC).date()


def ensure_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime, or convert an aware one to UTC.

    External sources return naive timestamps constantly (CSV exports, some
    JSON feeds). Assuming UTC is the least-wrong interpretation and is
    applied in exactly one place so the assumption is visible.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def age(value: datetime) -> timedelta:
    """Time elapsed since `value`. Accepts naive input and treats it as UTC."""
    reference = ensure_utc(value)
    assert reference is not None
    return utc_now() - reference
