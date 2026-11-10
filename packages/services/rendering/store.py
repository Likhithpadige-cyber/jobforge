"""Artifact byte storage, behind an interface narrow enough to be honest.

Four operations. Anything wider would tempt a caller into bucket listing or
key enumeration, neither of which this system needs and both of which turn
the object store into a directory of who has applied where.

Ordering is the reason this interface has `put` *and* `verify`. The service
uploads first, reads the object back, compares its checksum, and only then
writes the database row. A crash between upload and insert leaves an orphan
object, which is garbage; a crash the other way round would leave a row that
claims an artifact a download will 404 on. The safe direction is the one
where the row's existence always implies the bytes exist, and the interface
makes the readback a first-class step rather than an optional nicety.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from packages.shared.errors import AppError, DependencyUnavailableError
from packages.shared.logging import get_logger

log = get_logger(__name__)


class ArtifactStoreUnavailableError(DependencyUnavailableError):
    """The configured store cannot be reached or its driver is missing."""


class ArtifactIntegrityError(AppError):
    """Stored bytes do not hash to what was uploaded, or to what was recorded.

    A 500 and an alert, never a served file. An artifact whose checksum does
    not match is either a truncated upload or a modified object, and a resume
    a candidate is about to send to an employer is precisely the document not
    to serve on a guess.
    """

    status_code = 500
    title = "Artifact Integrity Failure"
    error_type = "https://jobforge.local/errors/artifact-integrity"


@runtime_checkable
class ArtifactStore(Protocol):
    """Private object storage. Nothing here returns a URL, by design.

    There is no `presigned_url` and no `public_url`. A presigned URL is a
    bearer token for one person's resume, and once minted it works for anyone
    holding it until it expires; downloads stream through the authenticated
    API instead, where authorization is evaluated per request.
    """

    bucket: str

    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def verify(self, key: str, *, sha256: str) -> None:
        """Read back and compare. Raises `ArtifactIntegrityError` on mismatch."""
        ...


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilesystemArtifactStore:
    """Non-networked store for tests, local development and the single-host
    private runtime (M20), where `root` is a persistent named Docker volume
    shared by the API and render worker and backed up by M21.

    Writes through a temporary file in the same directory and an atomic
    rename, so a crash mid-write cannot leave a partial object under a key the
    database might later point at. That is the same property the S3 adapter
    gets from a single PUT being atomic, reproduced here rather than assumed,
    because the test suite is where the ordering guarantees are actually
    exercised.

    This is a real implementation, not a stub: it is the one the integration
    tests run against, and it must behave like the interface says.
    """

    def __init__(self, root: str | Path, *, bucket: str = "jobforge-artifacts-local") -> None:
        self.root = Path(root)
        self.bucket = bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # `keys.storage_key` already constrains what a key may contain, but a
        # store must not depend on its caller for containment.
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if not str(candidate).startswith(str(root) + os.sep):
            raise ValueError("artifact key escapes the store root")
        return candidate

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".partial")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        # Content type is not stored on disk; the database row is the record.
        log.debug("artifact.stored", key=key, bytes=len(data), content_type=content_type)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ArtifactStoreUnavailableError("artifact object is missing")
        return path.read_bytes()

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def verify(self, key: str, *, sha256: str) -> None:
        actual = _digest(await self.get(key))
        if actual != sha256:
            log.error("artifact.integrity_mismatch", key=key)
            raise ArtifactIntegrityError("stored artifact does not match its checksum")


class S3ArtifactStore:
    """Private S3/MinIO storage over boto3.

    Same endpoint-configurable client for both targets: MinIO locally, real
    S3 in production, distinguished only by `endpoint_url` (set for MinIO,
    left unset so the AWS SDK resolves its own regional endpoint). Credential
    resolution follows boto3's own chain whenever `access_key_id` /
    `secret_access_key` are not given explicitly - env vars, a shared
    credentials file, or an instance/task role - rather than this class
    inventing a second one.

    `put_object` is called with `ContentType` and never with an `ACL`
    argument. The bucket's access is governed by its own policy; an adapter
    that never names an ACL cannot flip an individual object to public by
    accident.

    `verify` re-downloads and re-hashes rather than trusting ETag. A
    multipart upload's ETag is not a hash of the object at all, and even for
    a single-part upload it is MD5, not the SHA-256 this system records - so
    comparing the two would be comparing different things and calling it a
    checksum.

    Every botocore exception is caught at the operation boundary and
    re-raised as `ArtifactStoreUnavailableError` with a message that says
    what failed, never the exception's own string: botocore embeds request
    parameters (and, on some failures, endpoint and host detail) in its
    exception text, and that is not something a client of this store should
    ever see.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
    ) -> None:
        self.bucket = bucket
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ArtifactStoreUnavailableError(
                "The S3 artifact store requires boto3, which is not installed"
            ) from exc
        try:
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region_name,
                aws_access_key_id=access_key_id or None,
                aws_secret_access_key=secret_access_key or None,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        except Exception as exc:
            # boto3.client() does not touch the network, but it does validate
            # its own arguments (a malformed endpoint URL, an unknown config
            # option), and that failure belongs to the same fail-closed path
            # as an unreachable one.
            log.error("artifact.store_misconfigured", bucket=bucket)
            raise ArtifactStoreUnavailableError(
                "The S3 artifact store could not be configured"
            ) from exc

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(self._put_sync, key, data, content_type)
        log.debug("artifact.stored", key=key, bytes=len(data), content_type=content_type)

    def _put_sync(self, key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as exc:
            log.error("artifact.store_unavailable", key=key, operation="put")
            raise ArtifactStoreUnavailableError("artifact store put failed") from exc

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, key)

    def _get_sync(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body: bytes = response["Body"].read()
            return body
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactStoreUnavailableError("artifact object is missing") from exc
            log.error("artifact.store_unavailable", key=key, operation="get")
            raise ArtifactStoreUnavailableError("artifact store get failed") from exc
        except BotoCoreError as exc:
            log.error("artifact.store_unavailable", key=key, operation="get")
            raise ArtifactStoreUnavailableError("artifact store get failed") from exc

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, key)

    def _exists_sync(self, key: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_missing(exc):
                return False
            # Not `return False`. A store that answers "no object here" while
            # unable to look is worse than one that refuses: it would let a
            # caller conclude an artifact is missing when the truth is
            # unknown.
            log.error("artifact.store_unavailable", key=key, operation="head")
            raise ArtifactStoreUnavailableError("artifact store head failed") from exc
        except BotoCoreError as exc:
            log.error("artifact.store_unavailable", key=key, operation="head")
            raise ArtifactStoreUnavailableError("artifact store head failed") from exc

    async def verify(self, key: str, *, sha256: str) -> None:
        actual = _digest(await self.get(key))
        if actual != sha256:
            log.error("artifact.integrity_mismatch", key=key)
            raise ArtifactIntegrityError("stored artifact does not match its checksum")


def _is_missing(exc: Any) -> bool:
    """True for the "no such object" shape of a botocore ClientError.

    MinIO and AWS S3 do not always agree on the error code for a missing key
    (`NoSuchKey` from GetObject, `404`/`Not Found` from HeadObject, which
    carries no `Error.Code` at all on some S3-compatible backends), so the
    HTTP status is checked as well as the code rather than the code alone.
    """
    response = getattr(exc, "response", None) or {}
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "404", "NotFound") or status == 404
