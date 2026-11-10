"""M21: backup encryption round-trips and refuses every kind of tampering."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from scripts import backup_crypto as bc

pytestmark = [pytest.mark.unit, pytest.mark.safety]

PASSPHRASE = b"synthetic passphrase 0001"


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    d = tmp_path_factory.mktemp("keys")
    public = bc.generate_keypair(d / "backup.key", d / "backup.pub", PASSPHRASE)
    return bc.load_public(public), bc.load_private((d / "backup.key").read_bytes(), PASSPHRASE), d


def _encrypt(data: bytes, public) -> bytes:  # type: ignore[no-untyped-def]
    out = io.BytesIO()
    assert bc.encrypt_stream(io.BytesIO(data), out, public, "test") == len(data)
    return out.getvalue()


def _decrypt(blob: bytes, private) -> bytes:  # type: ignore[no-untyped-def]
    return b"".join(bc.decrypt_stream(io.BytesIO(blob), private))


@pytest.mark.parametrize("size", [0, 1, bc.CHUNK - 1, bc.CHUNK, bc.CHUNK + 1, 3 * bc.CHUNK + 7])
def test_round_trip_at_chunk_boundaries(keys, size: int) -> None:  # type: ignore[no-untyped-def]
    public, private, _ = keys
    data = os.urandom(size)
    blob = _encrypt(data, public)
    assert _decrypt(blob, private) == data
    assert bc.inspect(io.BytesIO(blob))["chunks"] == max(1, -(-size // bc.CHUNK))


def test_iterable_source_matches_file_source(keys) -> None:  # type: ignore[no-untyped-def]
    public, private, _ = keys
    data = os.urandom(2 * bc.CHUNK + 5)
    out = io.BytesIO()
    pieces = (data[i : i + 7000] for i in range(0, len(data), 7000))
    bc.encrypt_stream(pieces, out, public, "iter")
    assert _decrypt(out.getvalue(), private) == data


def test_each_file_gets_a_fresh_key(keys) -> None:  # type: ignore[no-untyped-def]
    public, _, _ = keys
    assert _encrypt(b"same", public) != _encrypt(b"same", public)


def test_key_files_are_private_and_never_overwritten(keys) -> None:  # type: ignore[no-untyped-def]
    _, _, d = keys
    assert stat.S_IMODE((d / "backup.key").stat().st_mode) == 0o600
    assert b"ENCRYPTED PRIVATE KEY" in (d / "backup.key").read_bytes()
    with pytest.raises(bc.BackupCryptoError, match="overwrite"):
        bc.generate_keypair(d / "backup.key", d / "other.pub", PASSPHRASE)
    with pytest.raises(bc.BackupCryptoError, match="at least 12"):
        bc.generate_keypair(d / "x.key", d / "x.pub", b"short")


def test_wrong_passphrase_and_wrong_key_fail(keys, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    public, _, d = keys
    with pytest.raises(bc.BackupCryptoError, match="could not be opened"):
        bc.load_private((d / "backup.key").read_bytes(), b"wrong passphrase!!")
    bc.generate_keypair(tmp_path / "o.key", tmp_path / "o.pub", PASSPHRASE)
    other = bc.load_private((tmp_path / "o.key").read_bytes(), PASSPHRASE)
    with pytest.raises(bc.BackupCryptoError, match="authentication failed"):
        _decrypt(_encrypt(b"secret data", public), other)


def _frames(blob: bytes) -> tuple[bytes, list[bytes]]:
    head_len = int.from_bytes(blob[len(bc.MAGIC) : len(bc.MAGIC) + 4], "big")
    start = len(bc.MAGIC) + 4 + head_len
    frames, pos = [], start
    while pos < len(blob):
        length = int.from_bytes(blob[pos : pos + 4], "big")
        frames.append(blob[pos : pos + 5 + length])
        pos += 5 + length
    return blob[:start], frames


def test_tampering_is_always_detected(keys) -> None:  # type: ignore[no-untyped-def]
    public, private, _ = keys
    blob = _encrypt(os.urandom(3 * bc.CHUNK), public)
    header, frames = _frames(blob)
    assert len(frames) == 3

    flipped = bytearray(blob)
    flipped[-20] ^= 0x01
    variants = {
        "bit flip in body": bytes(flipped),
        "chunks reordered": header + frames[1] + frames[0] + frames[2],
        "middle chunk dropped": header + frames[0] + frames[2],
        "header label edited": blob.replace(b'"label": "test"', b'"label": "tset"', 1),
        "final flag moved": header + frames[0] + frames[1][:4] + b"\1" + frames[1][5:],
    }
    for name, variant in variants.items():
        with pytest.raises(bc.BackupCryptoError):
            _decrypt(variant, private)
        assert name  # documents which case failed if this ever regresses


def test_truncation_is_detected_without_the_key(keys) -> None:  # type: ignore[no-untyped-def]
    public, private, _ = keys
    blob = _encrypt(os.urandom(2 * bc.CHUNK), public)
    header, frames = _frames(blob)
    for truncated in (header + frames[0], blob[:-1], blob[: len(header) + 3]):
        with pytest.raises(bc.BackupCryptoError):
            bc.inspect(io.BytesIO(truncated))
        with pytest.raises(bc.BackupCryptoError):
            _decrypt(truncated, private)
    with pytest.raises(bc.BackupCryptoError, match="after the final"):
        bc.inspect(io.BytesIO(blob + frames[0]))


def test_garbage_is_rejected(keys) -> None:  # type: ignore[no-untyped-def]
    _, private, _ = keys
    for junk in (b"", b"PGDMP plaintext dump", bc.MAGIC + b"\xff\xff\xff\xff"):
        with pytest.raises(bc.BackupCryptoError):
            bc.inspect(io.BytesIO(junk))
        with pytest.raises(bc.BackupCryptoError):
            _decrypt(junk, private)
    with pytest.raises(bc.BackupCryptoError):
        bc.load_public("not a key")
