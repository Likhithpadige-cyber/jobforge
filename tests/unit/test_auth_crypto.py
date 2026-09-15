"""TOTP secret encryption (AES-256-GCM) and password hashing.

These are the primitives every later security guarantee rests on, so the
negative cases matter more than the positive one: a decrypt that quietly
succeeds on a tampered blob would defeat the whole column.
"""

from __future__ import annotations

import base64
import os

import pytest

from packages.core.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from packages.core.crypto import (
    TOTP_BLOB_VERSION,
    DecryptionError,
    MissingEncryptionKeyError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_encryption_key,
)

pytestmark = [pytest.mark.unit, pytest.mark.safety]

SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def key() -> str:
    return generate_encryption_key()


# --- key handling -----------------------------------------------------------


def test_generated_key_is_32_bytes_base64(key: str) -> None:
    assert len(base64.b64decode(key, validate=True)) == 32


@pytest.mark.parametrize(
    "bad",
    ["", "not-base64!!", base64.b64encode(os.urandom(16)).decode()],
)
def test_unusable_key_fails_closed(bad: str) -> None:
    """An unset or wrong-sized key is a configuration fault, not a silent no-op."""
    with pytest.raises(MissingEncryptionKeyError):
        encrypt_totp_secret(SECRET, bad)


# --- round trip -------------------------------------------------------------


def test_round_trip(key: str) -> None:
    assert decrypt_totp_secret(encrypt_totp_secret(SECRET, key), key) == SECRET


def test_blob_layout_is_version_nonce_ciphertext(key: str) -> None:
    blob = encrypt_totp_secret(SECRET, key)
    assert blob[0] == TOTP_BLOB_VERSION
    # 1 version + 12 nonce + ciphertext(len(secret)) + 16 tag
    assert len(blob) == 1 + 12 + len(SECRET) + 16


def test_same_secret_encrypts_differently_each_time(key: str) -> None:
    """A fresh nonce per call, so the column leaks nothing by comparison."""
    first = encrypt_totp_secret(SECRET, key)
    second = encrypt_totp_secret(SECRET, key)
    assert first != second
    assert first[1:13] != second[1:13]  # nonces differ
    assert decrypt_totp_secret(first, key) == decrypt_totp_secret(second, key)


def test_plaintext_never_appears_in_the_blob(key: str) -> None:
    assert SECRET.encode() not in encrypt_totp_secret(SECRET, key)


def test_empty_secret_refused(key: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - ValidationError subclass
        encrypt_totp_secret("", key)


# --- fail closed ------------------------------------------------------------


def test_wrong_key_fails(key: str) -> None:
    blob = encrypt_totp_secret(SECRET, key)
    with pytest.raises(DecryptionError):
        decrypt_totp_secret(blob, generate_encryption_key())


def test_tampered_ciphertext_fails(key: str) -> None:
    blob = bytearray(encrypt_totp_secret(SECRET, key))
    blob[-1] ^= 0x01  # flip one bit of the tag
    with pytest.raises(DecryptionError):
        decrypt_totp_secret(bytes(blob), key)


def test_tampered_nonce_fails(key: str) -> None:
    blob = bytearray(encrypt_totp_secret(SECRET, key))
    blob[5] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt_totp_secret(bytes(blob), key)


def test_tampered_version_byte_fails(key: str) -> None:
    """The version is authenticated as associated data, not just read."""
    blob = bytearray(encrypt_totp_secret(SECRET, key))
    blob[0] = 0x02
    with pytest.raises(DecryptionError):
        decrypt_totp_secret(bytes(blob), key)


@pytest.mark.parametrize(
    "blob",
    [b"", b"\x01", b"\x01" + b"\x00" * 5, b"\x00" * 29, "not bytes"],
)
def test_malformed_blob_fails(blob: object, key: str) -> None:
    with pytest.raises(DecryptionError):
        decrypt_totp_secret(blob, key)  # type: ignore[arg-type]


def test_errors_never_leak_the_secret_or_key(key: str) -> None:
    """A traceback must not become the disclosure path."""
    blob = bytearray(encrypt_totp_secret(SECRET, key))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError) as exc:
        decrypt_totp_secret(bytes(blob), key)
    message = str(exc.value)
    assert SECRET not in message
    assert key not in message


def test_failures_are_indistinguishable(key: str) -> None:
    """Wrong key and tampered blob report the same thing - no oracle."""
    good = encrypt_totp_secret(SECRET, key)
    tampered = bytearray(good)
    tampered[-1] ^= 0x01

    with pytest.raises(DecryptionError) as wrong_key:
        decrypt_totp_secret(good, generate_encryption_key())
    with pytest.raises(DecryptionError) as bad_tag:
        decrypt_totp_secret(bytes(tampered), key)
    assert str(wrong_key.value) == str(bad_tag.value)


# --- passwords --------------------------------------------------------------


def test_hash_does_not_preserve_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")
    assert "correct horse battery staple" not in hashed
    assert hashed.startswith("$argon2id$")


def test_valid_password_verifies() -> None:
    assert verify_password("s3cret-passphrase", hash_password("s3cret-passphrase"))


def test_invalid_password_rejected() -> None:
    assert not verify_password("wrong", hash_password("s3cret-passphrase"))


def test_same_password_hashes_differently() -> None:
    """Per-hash salt, so equal passwords are not equal in the column."""
    assert hash_password("same-password") != hash_password("same-password")


@pytest.mark.parametrize("stored", [None, "", "not-a-phc-string"])
def test_missing_or_corrupt_hash_never_authenticates(stored: str | None) -> None:
    """A seeded user with no credentials set cannot sign in."""
    assert not verify_password("anything", stored)


def test_empty_password_never_authenticates() -> None:
    assert not verify_password("", hash_password("s3cret-passphrase"))


def test_empty_password_refused_at_hash_time() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_current_parameters_do_not_need_rehash() -> None:
    assert not needs_rehash(hash_password("s3cret-passphrase"))
