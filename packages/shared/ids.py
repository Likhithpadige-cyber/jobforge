"""UUIDv7 generation.

UUIDv7 is time-ordered, which keeps B-tree primary key indexes from
fragmenting the way random UUIDv4 keys do. DATABASE_DESIGN.md §1 specifies
UUIDv7 generated in application code.

Python's stdlib has no uuid7 yet (PEP-pending), so this is a compact
RFC 9562 §5.7 implementation with no third-party dependency.
"""

from __future__ import annotations

import os
import time
import uuid

_LAST_MS = 0
_COUNTER = 0


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7.

    Layout: 48-bit millisecond timestamp | version | 12-bit monotonic
    counter | variant | 62 random bits.
    """
    global _LAST_MS, _COUNTER

    now_ms = time.time_ns() // 1_000_000

    if now_ms > _LAST_MS:
        # New millisecond. Seed the counter in its lower quarter (RFC 9562
        # §6.2 "fixed-length dedicated counter seeding") so that a burst of
        # IDs inside one millisecond has ~3000 values of headroom before it
        # can roll over.
        _LAST_MS = now_ms
        _COUNTER = int.from_bytes(os.urandom(2), "big") & 0x03FF
    else:
        # Same millisecond, or the wall clock went backwards (NTP step).
        # Either way we advance our own counter and never emit a timestamp
        # lower than the last one - monotonicity is the whole point of v7.
        _COUNTER += 1
        if _COUNTER > 0x0FFF:
            _LAST_MS += 1
            _COUNTER = 0

    ms = _LAST_MS
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= _COUNTER << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())


def timestamp_ms(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"expected a UUIDv7, got version {value.version}")
    return value.int >> 80
