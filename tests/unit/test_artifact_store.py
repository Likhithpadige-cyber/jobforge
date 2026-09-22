"""The filesystem artifact store, and the ordering guarantees it must supply.

`FilesystemArtifactStore` is a real implementation, not a stub: it is what the
integration tests run against, so it has to behave the way the interface
promises - atomically, with containment, and with a checksum that is compared
rather than assumed.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from packages.services.rendering.store import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreUnavailableError,
    FilesystemArtifactStore,
    S3ArtifactStore,
)

pytestmark = pytest.mark.unit

KEY = "resumes/0192f000-0000-7000-8000-000000000001/ats-single-column-1/resume.txt"
DATA = b"synthetic artifact bytes"
DIGEST = hashlib.sha256(DATA).hexdigest()
TEXT = "text/plain; charset=utf-8"


@pytest.fixture
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "artifacts")


def test_it_satisfies_the_protocol(store: FilesystemArtifactStore) -> None:
    assert isinstance(store, ArtifactStore)


async def test_round_trip(store: FilesystemArtifactStore) -> None:
    await store.put(KEY, DATA, content_type=TEXT)
    assert await store.get(KEY) == DATA
    assert await store.exists(KEY) is True


async def test_a_missing_object_is_not_silently_empty(store: FilesystemArtifactStore) -> None:
    """Returning b"" would let a caller store a zero-byte artifact as if it
    were real; a missing object is an error."""
    assert await store.exists(KEY) is False
    with pytest.raises(ArtifactStoreUnavailableError):
        await store.get(KEY)


async def test_verify_accepts_a_matching_checksum(store: FilesystemArtifactStore) -> None:
    await store.put(KEY, DATA, content_type=TEXT)
    await store.verify(KEY, sha256=DIGEST)


async def test_verify_refuses_a_mismatch(store: FilesystemArtifactStore) -> None:
    """The readback step that makes upload-then-insert trustworthy.

    A truncated or modified object must fail here, before any row claims the
    artifact is available.
    """
    await store.put(KEY, DATA, content_type=TEXT)
    with pytest.raises(ArtifactIntegrityError):
        await store.verify(KEY, sha256="0" * 64)


async def test_verify_detects_a_tampered_object(
    store: FilesystemArtifactStore, tmp_path: Path
) -> None:
    await store.put(KEY, DATA, content_type=TEXT)
    (tmp_path / "artifacts" / KEY).write_bytes(b"replaced")
    with pytest.raises(ArtifactIntegrityError):
        await store.verify(KEY, sha256=DIGEST)


async def test_a_rewrite_is_atomic_and_leaves_no_partial_file(
    store: FilesystemArtifactStore, tmp_path: Path
) -> None:
    """No `.partial` may survive a completed write.

    A partial file under a key the database points at is the exact failure
    the ordering rules exist to prevent.
    """
    await store.put(KEY, DATA, content_type=TEXT)
    await store.put(KEY, b"second version", content_type=TEXT)
    assert await store.get(KEY) == b"second version"
    leftovers = list((tmp_path / "artifacts").rglob("*.partial"))
    assert leftovers == []


@pytest.mark.parametrize(
    "escaping",
    ["../outside.txt", "a/../../outside.txt", "/etc/passwd"],
)
async def test_a_key_cannot_escape_the_store_root(
    store: FilesystemArtifactStore, escaping: str
) -> None:
    """Containment is the store's own job.

    `keys.storage_key` already constrains what it produces, but a store that
    trusts its caller for containment is one refactor away from writing
    outside its root.
    """
    with pytest.raises(ValueError):
        await store.put(escaping, DATA, content_type=TEXT)


def test_the_store_exposes_no_url_minting(store: FilesystemArtifactStore) -> None:
    """A presigned URL is a bearer token for one person's resume.

    Downloads go through the authenticated API instead, so the interface must
    not offer a way to mint a link at all.
    """
    for forbidden in ("presigned_url", "public_url", "url_for", "generate_url"):
        assert not hasattr(store, forbidden)


def test_the_s3_adapter_exposes_no_url_minting() -> None:
    s3_store = S3ArtifactStore(
        bucket="jobforge-artifacts-test",
        endpoint_url="http://localhost:9000",
        access_key_id="synthetic-key",
        secret_access_key="synthetic-secret",  # pragma: allowlist secret
    )
    for forbidden in ("presigned_url", "public_url", "url_for", "generate_url"):
        assert not hasattr(s3_store, forbidden)


# ------------------------------------------------------------ S3ArtifactStore
#
# No real AWS or MinIO here: `botocore.stub.Stubber` intercepts the client at
# the point boto3 would otherwise make an HTTP call, so these run offline and
# fast. They prove the adapter's own logic - which parameters it sends, how it
# translates a failure - not that a real object store agrees with it. That
# proof is `tests/integration/test_s3_artifact_store.py`, against real MinIO.

S3_BUCKET = "jobforge-artifacts-test"


@pytest.fixture
def s3_store() -> S3ArtifactStore:
    """Synthetic endpoint and synthetic credentials; never real AWS ones."""
    return S3ArtifactStore(
        bucket=S3_BUCKET,
        endpoint_url="http://localhost:9000",
        region_name="us-east-1",
        access_key_id="synthetic-key",
        secret_access_key="synthetic-secret",  # pragma: allowlist secret
    )


@pytest.fixture
def stubber(s3_store: S3ArtifactStore):  # type: ignore[no-untyped-def]
    stub = Stubber(s3_store._client)
    stub.activate()
    yield stub
    stub.assert_no_pending_responses()
    stub.deactivate()


def test_s3_store_satisfies_the_protocol(s3_store: S3ArtifactStore) -> None:
    assert isinstance(s3_store, ArtifactStore)


def test_s3_client_is_configured_from_the_given_settings(s3_store: S3ArtifactStore) -> None:
    """Endpoint and region reach the client, so MinIO and AWS share one path."""
    meta = s3_store._client.meta
    assert meta.region_name == "us-east-1"
    assert meta.endpoint_url == "http://localhost:9000"
    assert s3_store.bucket == S3_BUCKET


def test_s3_client_falls_back_to_the_default_credential_chain_when_unset() -> None:
    """No explicit key/secret must not become the literal strings "" and "".

    Passing empty strings as credentials to boto3 would attempt (and fail)
    SigV4 signing with an empty access key, instead of letting boto3 resolve
    real credentials from the environment, a shared file, or an instance
    role - which is what a production deployment without MinIO-style static
    keys actually needs.
    """
    s3_store = S3ArtifactStore(bucket=S3_BUCKET, endpoint_url="http://localhost:9000")
    credentials = s3_store._client._request_signer._credentials
    assert credentials is None or credentials.access_key != ""


async def test_s3_put_sends_content_type_and_no_acl(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    """The bucket stays private: an adapter that never names an ACL cannot
    accidentally make an object public.

    `expected_params` below is exact: `Stubber` fails the call if the real
    client sends any parameter not listed here, so the absence of `"ACL"` is
    itself the assertion.
    """
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": S3_BUCKET, "Key": KEY, "Body": DATA, "ContentType": TEXT},
    )
    await s3_store.put(KEY, DATA, content_type=TEXT)


async def test_s3_get_round_trips_exact_bytes(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    body = StreamingBody(io.BytesIO(DATA), len(DATA))
    stubber.add_response(
        "get_object",
        {"Body": body, "ContentType": TEXT},
        {"Bucket": S3_BUCKET, "Key": KEY},
    )
    assert await s3_store.get(KEY) == DATA


async def test_s3_exists_true_for_a_present_object(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    stubber.add_response("head_object", {"ContentType": TEXT}, {"Bucket": S3_BUCKET, "Key": KEY})
    assert await s3_store.exists(KEY) is True


async def test_s3_exists_false_for_a_missing_object(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    """A 404 HeadObject answers "no", not "unavailable"."""
    stubber.add_client_error(
        "head_object", service_error_code="404", http_status_code=404
    )
    assert await s3_store.exists(KEY) is False


async def test_s3_get_of_a_missing_object_fails_closed(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    """A missing GetObject is the store's own "unavailable", never b""."""
    stubber.add_client_error(
        "get_object", service_error_code="NoSuchKey", http_status_code=404
    )
    with pytest.raises(ArtifactStoreUnavailableError):
        await s3_store.get(KEY)


async def test_s3_verify_accepts_a_matching_checksum(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    body = StreamingBody(io.BytesIO(DATA), len(DATA))
    stubber.add_response("get_object", {"Body": body}, {"Bucket": S3_BUCKET, "Key": KEY})
    await s3_store.verify(KEY, sha256=DIGEST)


async def test_s3_verify_refuses_a_mismatch(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    """Re-hashes the object rather than trusting ETag.

    ETag is MD5 for a single-part upload and not a content hash at all for a
    multipart one, so it is never compared against `sha256` anywhere in this
    adapter - only a fresh download is.
    """
    tampered = b"a different body entirely"
    body = StreamingBody(io.BytesIO(tampered), len(tampered))
    stubber.add_response("get_object", {"Body": body}, {"Bucket": S3_BUCKET, "Key": KEY})
    with pytest.raises(ArtifactIntegrityError):
        await s3_store.verify(KEY, sha256=DIGEST)


async def test_s3_a_transport_failure_fails_closed_without_leaking_detail(
    s3_store: S3ArtifactStore, stubber: Stubber
) -> None:
    """Every botocore exception becomes `ArtifactStoreUnavailableError`.

    The synthetic AccessDenied message carries a fake resource identifier, to
    prove the adapter's own message is what reaches a caller - not
    botocore's, which can embed request parameters and endpoint detail.
    """
    stubber.add_client_error(
        "put_object",
        service_error_code="AccessDenied",
        service_message="arn:aws:s3:::do-not-leak-this-bucket-detail",
        http_status_code=403,
    )
    with pytest.raises(ArtifactStoreUnavailableError) as exc_info:
        await s3_store.put(KEY, DATA, content_type=TEXT)
    assert "do-not-leak-this-bucket-detail" not in str(exc_info.value)
    assert "AccessDenied" not in str(exc_info.value)


def test_s3_backend_selection_never_falls_back_to_the_filesystem() -> None:
    """`artifact_storage_backend="s3"` must construct an S3ArtifactStore.

    Never `FilesystemArtifactStore`, even implicitly - a deployment pointed
    at S3 that silently wrote to local disk would lose every artifact on the
    container's next restart while believing it had durable storage.
    """
    from apps.api.deps import artifact_store as api_artifact_store
    from packages.shared.config import Settings
    from workers.runtime import build_artifact_store as worker_artifact_store

    settings = Settings(
        artifact_storage_backend="s3",
        s3_bucket=S3_BUCKET,
        s3_endpoint_url="http://localhost:9000",
        s3_access_key_id="synthetic-key",
        s3_secret_access_key="synthetic-secret",  # pragma: allowlist secret
    )
    for build in (api_artifact_store, worker_artifact_store):
        built = build(settings)
        assert isinstance(built, S3ArtifactStore)
        assert not isinstance(built, FilesystemArtifactStore)
