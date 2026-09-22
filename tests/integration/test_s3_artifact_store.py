"""S3ArtifactStore against a real MinIO, not a mock.

`tests/unit/test_artifact_store.py` proves the adapter sends the parameters
it should and translates botocore's exceptions the way it should, entirely
offline through `botocore.stub.Stubber`. That is necessary and not
sufficient: a stub agrees with whatever the code under test tells it to
expect. This file runs the same adapter against a real S3-compatible server -
the MinIO started by `docker compose --profile full up -d minio` - so a wrong
assumption about how S3 actually behaves (content-type handling, what a
missing key really raises, whether bytes survive the wire unchanged) has
something independent to disagree with.

MinIO only. Nothing here is run against, or claimed to verify, real AWS S3.
The two are wire-compatible for the operations this adapter uses, which is
what makes MinIO a legitimate substitute for local development and CI - but
"passes against MinIO" and "passes against AWS S3" are different claims, and
only the first one is made here.

Synthetic credentials (the docker-compose default `minioadmin`/`minioadmin`)
and synthetic bytes throughout. Nothing in this file is a real resume.

Skips cleanly, the same way the PostgreSQL integration tests do, when MinIO
is not reachable - never a failure, because most local and CI runs do not
start the `full` profile.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from packages.services.rendering.store import (
    ArtifactIntegrityError,
    ArtifactStoreUnavailableError,
    S3ArtifactStore,
)
from packages.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.minio]

DATA = b"synthetic MinIO integration payload \x00\x01\x02 not a real resume"
CONTENT_TYPE = "text/plain; charset=utf-8"


def _new_store() -> S3ArtifactStore:
    settings = get_settings()
    return S3ArtifactStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )


@pytest.fixture
def store():  # type: ignore[no-untyped-def]
    """A store pointed at the configured MinIO, its bucket ensured to exist.

    No provisioning container or bootstrap script exists in this repository
    for MinIO - unlike PostgreSQL, which is provisioned once by `make
    bootstrap` and then assumed. The smallest equivalent here is exactly what
    the PostgreSQL fixtures already do for a missing precondition: create
    what this test run needs, in the fixture, and skip if the server itself
    is unreachable. This does not make the application create buckets at
    runtime - `create_bucket` is called from the test only.
    """
    settings = get_settings()
    candidate = _new_store()
    try:
        candidate._client.head_bucket(Bucket=candidate.bucket)
    except Exception:  # any failure here means "not ready"
        try:
            candidate._client.create_bucket(Bucket=candidate.bucket)
        except Exception as exc:
            pytest.skip(
                f"MinIO not reachable at {settings.s3_endpoint_url} - "
                f"start it with `docker compose --profile full up -d minio` ({exc})"
            )
    return candidate


def _key(name: str) -> str:
    # A fresh key per test, in the same shape `keys.storage_key` produces, so
    # one test's object can never mask another's and a re-run never collides
    # with a leftover from a previous one.
    return f"resumes/{uuid.uuid4()}/s3-integration-test/{name}"


# ----------------------------------------------------------- the happy path
async def test_put_then_exists_then_get_round_trips_exact_bytes(store) -> None:  # type: ignore[no-untyped-def]
    """`put -> exists/head -> get`, against a real object store."""
    key = _key("resume.txt")

    assert await store.exists(key) is False

    await store.put(key, DATA, content_type=CONTENT_TYPE)

    assert await store.exists(key) is True
    fetched = await store.get(key)
    assert fetched == DATA, "the exact bytes uploaded must be the exact bytes read back"


async def test_content_type_is_preserved(store) -> None:  # type: ignore[no-untyped-def]
    key = _key("resume.txt")
    await store.put(key, DATA, content_type=CONTENT_TYPE)
    head = store._client.head_object(Bucket=store.bucket, Key=key)
    assert head["ContentType"] == CONTENT_TYPE


async def test_verify_accepts_a_matching_checksum(store) -> None:  # type: ignore[no-untyped-def]
    key = _key("resume.txt")
    await store.put(key, DATA, content_type=CONTENT_TYPE)
    await store.verify(key, sha256=hashlib.sha256(DATA).hexdigest())


async def test_verify_refuses_a_mismatch(store) -> None:  # type: ignore[no-untyped-def]
    key = _key("resume.txt")
    await store.put(key, DATA, content_type=CONTENT_TYPE)
    with pytest.raises(ArtifactIntegrityError):
        await store.verify(key, sha256="0" * 64)


async def test_a_rewrite_under_the_same_key_replaces_the_object(store) -> None:  # type: ignore[no-untyped-def]
    """The deterministic-key overwrite semantics `render_resume` relies on
    for recovery (`FilesystemArtifactStore` gets this from an atomic rename;
    a single PUT to S3/MinIO is atomic the same way)."""
    key = _key("resume.txt")
    await store.put(key, DATA, content_type=CONTENT_TYPE)
    second = b"a completely different synthetic payload"
    await store.put(key, second, content_type=CONTENT_TYPE)
    assert await store.get(key) == second


# ------------------------------------------------------------- missing object
async def test_exists_is_false_for_an_object_that_was_never_uploaded(store) -> None:  # type: ignore[no-untyped-def]
    assert await store.exists(_key("never-uploaded.txt")) is False


async def test_get_of_a_missing_object_fails_closed(store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ArtifactStoreUnavailableError):
        await store.get(_key("never-uploaded.txt"))


# -------------------------------------------------------- a meaningful outage
async def test_an_unreachable_endpoint_fails_closed_through_the_stores_own_error() -> None:
    """Wrong endpoint, not merely a wrong key - the transport-failure path,
    not the missing-object path. Short timeouts so this does not hang the
    suite while nothing is listening on the unused port."""
    import boto3
    from botocore.config import Config

    settings = get_settings()
    unreachable = S3ArtifactStore.__new__(S3ArtifactStore)
    unreachable.bucket = settings.s3_bucket
    unreachable._client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        region_name=settings.s3_region,
        aws_access_key_id="synthetic-key",
        aws_secret_access_key="synthetic-secret",  # pragma: allowlist secret
        config=Config(
            signature_version="s3v4",
            connect_timeout=1,
            read_timeout=1,
            retries={"max_attempts": 1},
        ),
    )
    with pytest.raises(ArtifactStoreUnavailableError):
        await unreachable.put(_key("unreachable.txt"), DATA, content_type=CONTENT_TYPE)
