"""Streaming public-key encryption for JobForge backups (M21).

Construction (no new dependency; `cryptography` is already pinned):

- Recipient key pair: X25519. Only the PUBLIC key is needed to make a
  backup, so the Mac never holds what decrypts its own backups. The private
  key is written once, encrypted with a passphrase (PKCS#8,
  BestAvailableEncryption), for the owner to keep offline.
- Per file: a fresh ephemeral X25519 key; file key = HKDF-SHA256(shared
  secret, salt = ephemeral_pub || recipient_pub, info = "jobforge-backup v1").
  Every file therefore has its own key.
- Body: AES-256-GCM over 1 MiB chunks. Nonce = 4 zero bytes || 8-byte chunk
  counter (unique because the key is per file). AAD = header || counter ||
  final-flag, so reordering, truncation, chunk dropping, header edits and a
  missing final chunk all fail authentication.

File layout: MAGIC | u32 header length | header JSON | repeated
(u32 ciphertext length | flag byte | ciphertext). `inspect()` checks framing
and the final marker without any key (used by backup verification on the
host); only `decrypt_stream()` with the private key proves authenticity.
"""

from __future__ import annotations

import base64
import json
import os
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"JFBACKUP1\n"
INFO = b"jobforge-backup v1"
CHUNK = 1024 * 1024
_FRAME = struct.Struct(">IB")
_LEN = struct.Struct(">I")
MAX_HEADER = 64 * 1024


class BackupCryptoError(Exception):
    """Malformed, tampered, truncated or wrong-key input. Never carries data."""


def generate_keypair(private_path: Path, public_path: Path, passphrase: bytes) -> str:
    """Write a new key pair; refuses to overwrite. Returns the public key (b64)."""
    if len(passphrase) < 12:
        raise BackupCryptoError("the passphrase must be at least 12 characters")
    for path in (private_path, public_path):
        if path.exists():
            raise BackupCryptoError(f"refusing to overwrite {path.name}")
    key = X25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    public_path.write_text(public + "\n")
    return public


def load_public(text: str) -> X25519PublicKey:
    try:
        return X25519PublicKey.from_public_bytes(base64.b64decode(text.strip(), validate=True))
    except (ValueError, TypeError) as exc:
        raise BackupCryptoError("not a JobForge backup public key") from exc


def load_private(pem: bytes, passphrase: bytes) -> X25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=passphrase)
    except (ValueError, TypeError) as exc:
        raise BackupCryptoError("the private key could not be opened") from exc
    if not isinstance(key, X25519PrivateKey):
        raise BackupCryptoError("not a JobForge backup private key")
    return key


def _file_key(shared: bytes, eph_pub: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=eph_pub + recipient_pub, info=INFO
    ).derive(shared)


def _nonce(counter: int) -> bytes:
    return b"\0\0\0\0" + counter.to_bytes(8, "big")


def _aad(header: bytes, counter: int, final: bool) -> bytes:
    return header + counter.to_bytes(8, "big") + (b"\1" if final else b"\0")


def _chunks(source: BinaryIO | Iterable[bytes]) -> Iterator[bytes]:
    if hasattr(source, "read"):
        while block := source.read(CHUNK):
            yield block
    else:
        buffer = b""
        for piece in source:
            buffer += piece
            while len(buffer) >= CHUNK:
                yield buffer[:CHUNK]
                buffer = buffer[CHUNK:]
        if buffer:
            yield buffer


def encrypt_stream(
    source: BinaryIO | Iterable[bytes], out: BinaryIO, recipient: X25519PublicKey, label: str
) -> int:
    """Encrypt `source` to `out`; returns the plaintext byte count."""
    ephemeral = X25519PrivateKey.generate()
    eph_pub = ephemeral.public_key().public_bytes_raw()
    rec_pub = recipient.public_bytes_raw()
    aes = AESGCM(_file_key(ephemeral.exchange(recipient), eph_pub, rec_pub))
    header = json.dumps(
        {
            "v": 1,
            "alg": "X25519-HKDF-SHA256/AES-256-GCM",
            "chunk": CHUNK,
            "eph": base64.b64encode(eph_pub).decode(),
            "label": label,
        },
        sort_keys=True,
    ).encode()
    out.write(MAGIC + _LEN.pack(len(header)) + header)
    total, counter = 0, 0
    iterator = _chunks(source)
    current = next(iterator, b"")
    while True:
        following = next(iterator, None)
        final = following is None
        sealed = aes.encrypt(_nonce(counter), current, _aad(header, counter, final))
        out.write(_FRAME.pack(len(sealed), 1 if final else 0) + sealed)
        total += len(current)
        counter += 1
        if following is None:
            return total
        current = following


def _read_header(src: BinaryIO) -> tuple[bytes, dict[str, object]]:
    if src.read(len(MAGIC)) != MAGIC:
        raise BackupCryptoError("not a JobForge encrypted backup")
    raw_len = src.read(_LEN.size)
    if len(raw_len) != _LEN.size:
        raise BackupCryptoError("truncated header")
    (length,) = _LEN.unpack(raw_len)
    if not 0 < length <= MAX_HEADER:
        raise BackupCryptoError("malformed header")
    header = src.read(length)
    try:
        meta = json.loads(header)
    except ValueError as exc:
        raise BackupCryptoError("malformed header") from exc
    if len(header) != length or meta.get("v") != 1:
        raise BackupCryptoError("unsupported or truncated header")
    return header, meta


def _frames(src: BinaryIO) -> Iterator[tuple[int, bool, bytes]]:
    counter, seen_final = 0, False
    while True:
        raw = src.read(_FRAME.size)
        if not raw:
            break
        if seen_final:
            raise BackupCryptoError("data after the final chunk")
        if len(raw) != _FRAME.size:
            raise BackupCryptoError("truncated chunk header")
        length, flag = _FRAME.unpack(raw)
        if length < 16 or length > CHUNK + 16 or flag not in (0, 1):
            raise BackupCryptoError("malformed chunk")
        sealed = src.read(length)
        if len(sealed) != length:
            raise BackupCryptoError("truncated chunk")
        seen_final = flag == 1
        yield counter, seen_final, sealed
        counter += 1
    if not seen_final:
        raise BackupCryptoError("missing final chunk (truncated backup)")


def inspect(src: BinaryIO) -> dict[str, object]:
    """Keyless structural check: header, framing, final marker. Not proof of
    authenticity - only decryption with the private key is."""
    _, meta = _read_header(src)
    chunks = sum(1 for _ in _frames(src))
    return {"label": meta.get("label"), "chunks": chunks}


def decrypt_stream(src: BinaryIO, private: X25519PrivateKey) -> Iterator[bytes]:
    """Yield authenticated plaintext chunks; raises on any tampering. A caller
    must not act on output until the generator finishes without raising."""
    header, meta = _read_header(src)
    try:
        eph_pub = base64.b64decode(str(meta["eph"]), validate=True)
        shared = private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    except (KeyError, ValueError, TypeError) as exc:
        raise BackupCryptoError("malformed header") from exc
    aes = AESGCM(_file_key(shared, eph_pub, private.public_key().public_bytes_raw()))
    for counter, final, sealed in _frames(src):
        try:
            yield aes.decrypt(_nonce(counter), sealed, _aad(header, counter, final))
        except InvalidTag as exc:
            raise BackupCryptoError(
                "authentication failed (tampered, reordered or wrong key)"
            ) from exc
