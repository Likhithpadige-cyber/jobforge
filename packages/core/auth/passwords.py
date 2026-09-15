"""Argon2id password hashing (SECURITY.md §3.1, DATABASE_DESIGN.md §6).

Thin wrapper over `argon2-cffi` so the parameters live in one place and the
rest of the system never touches a plaintext password beyond this module.

Nothing here logs, and no exception carries the password: a verification
failure returns `False` rather than raising with context, so a stray traceback
cannot leak the attempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argon2 import PasswordHasher

#: Argon2id parameters. Comfortably above the RFC 9106 low-memory profile and
#: cheap enough for an interactive login on a laptop.
TIME_COST = 3
MEMORY_COST_KIB = 64 * 1024  # 64 MiB
PARALLELISM = 4
HASH_LEN = 32
SALT_LEN = 16


def _hasher() -> PasswordHasher:
    from argon2 import PasswordHasher
    from argon2.low_level import Type

    return PasswordHasher(
        time_cost=TIME_COST,
        memory_cost=MEMORY_COST_KIB,
        parallelism=PARALLELISM,
        hash_len=HASH_LEN,
        salt_len=SALT_LEN,
        type=Type.ID,  # argon2id, not argon2i or argon2d
    )


def hash_password(password: str) -> str:
    """Hash a password for `users.password_hash`.

    The result is the standard PHC string, which carries its own salt and
    parameters, so a future parameter change stays verifiable against old
    hashes without a migration.
    """
    if not password:
        raise ValueError("Refusing to hash an empty password")
    return _hasher().hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-ish time verification. Returns False, never raises on mismatch.

    A user with no `password_hash` (the state every seeded user starts in)
    verifies as False rather than as an error, so an account that has never
    had credentials set simply cannot log in.
    """
    if not stored_hash or not password:
        return False
    from argon2.exceptions import (
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )

    try:
        return _hasher().verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when a stored hash predates the current parameters."""
    if not stored_hash:
        return False
    from argon2.exceptions import InvalidHashError

    try:
        return _hasher().check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False
