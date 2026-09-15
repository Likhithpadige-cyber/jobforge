"""Application-layer encryption for `users.totp_secret_enc` (AES-256-GCM).

DATABASE_DESIGN.md §6 records this column as "encrypted at app layer". This
module is that layer, and nothing else: it encrypts TOTP secrets and does not
sign sessions, hash passwords, or protect any other column. Mixing those
concerns would mean one key rotation silently breaking an unrelated subsystem.

**Byte layout** of the stored blob, exactly:

    ┌─────────┬──────────────┬────────────────────────────────┐
    │ version │    nonce     │  AES-256-GCM ciphertext ‖ tag  │
    │ 1 byte  │   12 bytes   │        len(plaintext) + 16     │
    └─────────┴──────────────┴────────────────────────────────┘

    offset 0       : version, currently 0x01
    offset 1..12   : random 96-bit nonce, fresh per encryption
    offset 13..end : ciphertext with the 128-bit GCM tag appended

The version byte is deliberate: the column is bare `BYTEA` with no `key_id`
and no version column, so without a prefix there would be no way to introduce
a second key or a different cipher later without a migration. One byte now
buys that option.

The version byte is also authenticated. It is passed to GCM as associated
data, so flipping it to point at a different scheme invalidates the tag rather
than silently changing how the blob is read.

Every failure path raises `DecryptionError` and none of them says why in a way
that distinguishes "wrong key" from "tampered" from "malformed" - and no
exception here ever carries plaintext, ciphertext or key material.

**Key management limitation.** In local development `TOTP_ENCRYPTION_KEY`
lives in `.env`, on the same machine as PostgreSQL. Against the threat
SECURITY.md §3.3 names - a database dump alone yielding usable credentials -
that is weak, because whoever obtains the dump very likely obtains `.env` too.
This is structural parity with the production design, not equivalent
protection. See SECURITY.md §3.4.
"""

from __future__ import annotations

import base64
import os

from packages.shared.errors import ValidationError

#: Current blob version. Bump only alongside a documented migration path.
TOTP_BLOB_VERSION = 1

_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # 96-bit nonce, the GCM-recommended size
_TAG_BYTES = 16
_MIN_BLOB = 1 + _NONCE_BYTES + _TAG_BYTES


class MissingEncryptionKeyError(ValidationError):
    """`TOTP_ENCRYPTION_KEY` is unset or not a valid 32-byte base64 key."""


class DecryptionError(ValidationError):
    """The blob could not be authenticated and decrypted.

    Raised identically for a wrong key, a tampered blob, an unsupported
    version and a malformed length, so the error cannot be used as an oracle
    to tell those cases apart.
    """


def generate_encryption_key() -> str:
    """A fresh base64 key for `TOTP_ENCRYPTION_KEY`, for setup tooling."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode()


def _key_bytes(key_b64: str) -> bytes:
    """Decode and validate the configured key, failing closed when unusable."""
    if not key_b64:
        raise MissingEncryptionKeyError(
            "TOTP_ENCRYPTION_KEY is not configured; run the auth setup command"
        )
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise MissingEncryptionKeyError(
            "TOTP_ENCRYPTION_KEY must be base64-encoded"
        ) from exc
    if len(raw) != _KEY_BYTES:
        raise MissingEncryptionKeyError(
            f"TOTP_ENCRYPTION_KEY must decode to {_KEY_BYTES} bytes"
        )
    return raw


def encrypt_totp_secret(secret: str, key_b64: str) -> bytes:
    """Encrypt a TOTP secret for storage in `users.totp_secret_enc`.

    A fresh random nonce per call, so encrypting the same secret twice yields
    different blobs and the column leaks nothing by comparison.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not secret:
        raise ValidationError("Refusing to encrypt an empty TOTP secret")
    version = bytes([TOTP_BLOB_VERSION])
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_key_bytes(key_b64)).encrypt(nonce, secret.encode("utf-8"), version)
    return version + nonce + sealed


def decrypt_totp_secret(blob: bytes, key_b64: str) -> str:
    """Recover a TOTP secret, or raise `DecryptionError`.

    Fails closed on every malformed, truncated, unsupported-version, tampered
    or wrong-key input. There is no partial success and no fallback path.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _key_bytes(key_b64)  # a missing key is a config fault, not a decrypt fault
    if not isinstance(blob, bytes | bytearray | memoryview):
        # Same message as every other failure: one error string for all of
        # them, so nothing here can be used to classify the input.
        raise DecryptionError("Stored TOTP secret could not be decrypted")
    blob = bytes(blob)
    if len(blob) < _MIN_BLOB or blob[0] != TOTP_BLOB_VERSION:
        # Length and version are checked together and reported identically, so
        # the error distinguishes nothing for an attacker.
        raise DecryptionError("Stored TOTP secret could not be decrypted")
    nonce = blob[1 : 1 + _NONCE_BYTES]
    sealed = blob[1 + _NONCE_BYTES :]
    try:
        plaintext = AESGCM(key).decrypt(nonce, sealed, blob[:1])
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("Stored TOTP secret could not be decrypted") from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptionError("Stored TOTP secret could not be decrypted") from exc
