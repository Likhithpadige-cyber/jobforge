"""M8 over HTTP and against the database.

Marked `safety`: the properties asserted here are the ones that stop an
unapproved, stale or fabricated document being handed to someone about to
send it to an employer, and stop one person's resume reaching another.

Synthetic content throughout. Nothing in this file resembles a real resume.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apps.api.deps import CSRF_HEADER
from packages.core.models import Approval, Artifact, Event, Resume
from packages.core.models.enums import ApprovalStatus
from packages.core.services.artifacts import (
    NotApprovedError,
    StaleApprovalError,
    render_resume,
)
from packages.services.rendering import TEMPLATE_VERSION
from packages.services.rendering.protocol import RendererUnavailableError
from packages.services.rendering.reference import ReferenceTextRenderer
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]

RATE_LIMIT_TYPE = "https://jobforge.local/errors/rate-limited"


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    return FilesystemArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def renderers():  # type: ignore[no-untyped-def]
    """The reference renderer, passed in explicitly.

    `render_resume` refuses a non-production renderer unless the caller opts
    in, which is exactly what stops a test double reaching a real artifact.
    Opting in here is the deliberate act that proves the guard exists.
    """
    return (ReferenceTextRenderer(TEMPLATE_VERSION),)


# ------------------------------------------------- approval is a precondition
async def test_an_unapproved_resume_is_not_rendered(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """No approval, no artifact. The gate is the human decision, not the
    verification verdict."""
    resume_id = approved_resume_fixtures["unapproved_resume_id"]
    with pytest.raises(NotApprovedError):
        await render_resume(
            session,
            store=store,
            renderers=renderers,
            resume_id=resume_id,
            allow_non_production_renderers=True,
        )


async def test_a_failed_verification_resume_is_never_rendered(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """The fabrication tripwire reaches M8 too.

    A resume that failed claim verification is reviewed by a human; it is not
    turned into something sendable.
    """
    resume_id = approved_resume_fixtures["failed_resume_id"]
    with pytest.raises(NotApprovedError):
        await render_resume(
            session,
            store=store,
            renderers=renderers,
            resume_id=resume_id,
            allow_non_production_renderers=True,
        )


async def test_an_approval_for_an_earlier_version_does_not_authorise_this_one(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """Stale-approval protection, reusing `snapshot_matches`.

    Matching on resume id alone would let a regenerated version inherit a
    decision made about its predecessor - the precise thing the subject
    snapshot exists to prevent.
    """
    resume_id = approved_resume_fixtures["superseded_resume_id"]
    with pytest.raises(NotApprovedError):
        await render_resume(
            session,
            store=store,
            renderers=renderers,
            resume_id=resume_id,
            allow_non_production_renderers=True,
        )


# ------------------------------------------------------------ the happy path
async def test_an_approved_resume_renders_and_records_its_binding(
    session, store, renderers, approved_resume_fixtures
) -> None:
    resume_id = approved_resume_fixtures["approved_resume_id"]
    produced = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    assert len(produced) == 1
    artifact = produced[0]

    assert artifact.resume_id == resume_id
    assert artifact.resume_version == approved_resume_fixtures["approved_version"]
    assert artifact.approval_id == approved_resume_fixtures["approval_id"]
    assert artifact.template_version == TEMPLATE_VERSION
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.size_bytes and artifact.size_bytes > 0


async def test_the_stored_object_matches_the_recorded_checksum(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """The readback that makes upload-then-insert trustworthy."""
    produced = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=approved_resume_fixtures["approved_resume_id"],
        allow_non_production_renderers=True,
    )
    artifact = produced[0]
    await store.verify(artifact.storage_key, sha256=artifact.sha256)


async def test_the_storage_key_carries_no_candidate_data(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """A key ends up in bucket listings and access logs."""
    produced = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=approved_resume_fixtures["approved_resume_id"],
        allow_non_production_renderers=True,
    )
    key = produced[0].storage_key
    assert key.startswith("resumes/")
    assert "@" not in key
    for word in approved_resume_fixtures["content_words"]:
        assert word.lower() not in key.lower()


async def test_rendering_never_invents_text(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """Every line in the artifact traces to persisted content or template
    chrome. M7 owns what the resume says; M8 only lays it out."""
    produced = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=approved_resume_fixtures["approved_resume_id"],
        allow_non_production_renderers=True,
    )
    body = (await store.get(produced[0].storage_key)).decode("utf-8")
    approved = approved_resume_fixtures["content_lines"]
    rendered_lines = [
        line[2:] for line in body.splitlines() if line.startswith("- ")
    ]
    assert sorted(rendered_lines) == sorted(approved)


# --------------------------------------------------------------- idempotency
async def test_replaying_the_render_writes_nothing_new(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """A redelivered event costs one lookup and produces nothing durable.

    "Nothing" includes the event. A replay that returned the existing rows
    and then emitted a second `resume.rendered` would tell every consumer a
    render had happened that never did, and the row count alone would not
    have caught it.
    """
    resume_id = approved_resume_fixtures["approved_resume_id"]
    kwargs = {
        "store": store,
        "renderers": renderers,
        "resume_id": resume_id,
        "allow_non_production_renderers": True,
    }
    first = await render_resume(session, **kwargs)
    await session.flush()
    _, after_first = await _durable_record(session, resume_id)
    assert len(after_first) == 1, "a first render emits exactly one rendered event"

    second = await render_resume(session, **kwargs)

    assert [a.id for a in first] == [a.id for a in second]
    _, after_replay = await _durable_record(session, resume_id)
    assert len(after_replay) == 1, "a pure replay emitted a second rendered event"
    rows = (
        await session.scalars(select(Artifact).where(Artifact.resume_id == resume_id))
    ).all()
    assert len(rows) == 1


# ------------------------------------------------------- production refusals
async def test_a_non_production_renderer_is_refused_by_default(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """The opt-in is what keeps a test double out of a real download."""
    with pytest.raises(RendererUnavailableError):
        await render_resume(
            session,
            store=store,
            renderers=renderers,
            resume_id=approved_resume_fixtures["approved_resume_id"],
        )


# ------------------------------------------------------- event idempotency
#: A second template, so one invocation can meet an artifact that already
#: exists and one it must create. The unique index is on
#: (resume_id, kind, template_version), so this is a new artifact rather than
#: a replacement for the first.
ALT_TEMPLATE_VERSION = f"{TEMPLATE_VERSION}-alt"


async def test_a_mixed_invocation_emits_one_event_covering_every_artifact(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """One existing artifact, one genuinely created: exactly one new event.

    The emission is gated on what this invocation persisted, so an invocation
    that creates anything emits once - not once per new artifact, and not at
    all when it created nothing. The payload is the other half of the
    contract: `resume.rendered` says what a consumer may now act on, so it
    names every artifact the resume has, the pre-existing one included.
    """
    resume_id = approved_resume_fixtures["approved_resume_id"]
    await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    rows, events = await _durable_record(session, resume_id)
    assert len(rows) == 1
    assert len(events) == 1

    both = (
        ReferenceTextRenderer(TEMPLATE_VERSION),
        ReferenceTextRenderer(ALT_TEMPLATE_VERSION),
    )
    produced = await render_resume(
        session,
        store=store,
        renderers=both,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    rows, events = await _durable_record(session, resume_id)

    assert len(produced) == 2
    assert len(rows) == 2, "the second template is a new artifact, not a replacement"
    assert len(events) == 2, "one further event, for the invocation that created one"

    listed = [{a["artifact_id"] for a in event.payload["artifacts"]} for event in events]
    assert {str(a.id) for a in produced} in listed, (
        "the new event must name every artifact the resume now has"
    )


# ------------------------------------------- the mid-flight approval races
#: A resume version the fixture never issues, so a mismatch here cannot be
#: confused with one of the other resumes it creates. The number is
#: irrelevant; that it differs from the approved snapshot is the point.
SUPERSEDING_VERSION = 99


class TripwireStore:
    """Changes the world in the window between upload and revalidation.

    `render_resume` uploads, verifies the checksum, revalidates, and only
    then writes the row. The race this exercises lives entirely inside that
    window, and the service already takes its store as a parameter - so the
    state transition is injected through a seat the production code provides,
    with no test-only hook added to it, no sleeping and no second connection.

    Delegates everything to a real `FilesystemArtifactStore`: the upload,
    the readback and the checksum comparison all genuinely happen, which is
    what makes the orphaned-object assertions afterwards mean something.
    """

    def __init__(self, inner, mutate) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self._mutate = mutate
        self.bucket = inner.bucket
        self.keys: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        await self._inner.put(key, data, content_type=content_type)
        self.keys.append(key)
        if len(self.keys) == 1:
            # Once only, so a later recovery render through the same store
            # is not sabotaged by the tripwire that caused the failure.
            await self._mutate()

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def verify(self, key: str, *, sha256: str) -> None:
        await self._inner.verify(key, sha256=sha256)


async def _durable_record(session, resume_id):  # type: ignore[no-untyped-def]
    """Rows and events for this resume. Both must be empty after a refusal."""
    artifacts = (
        await session.scalars(select(Artifact).where(Artifact.resume_id == resume_id))
    ).all()
    events = (
        await session.scalars(
            select(Event).where(
                Event.type == "resume.rendered", Event.aggregate_id == resume_id
            )
        )
    ).all()
    return artifacts, events


async def test_an_approved_decision_cannot_request_rework_mid_render(
    session, store, renderers, approved_resume_fixtures, demo_user,
) -> None:
    """The supported workflow and DB both refuse approved -> changes_requested.

    This is not a revocation race: no withdrawal workflow exists. A rejected
    rework attempt leaves the original authorization intact and rendering valid.
    The second production revalidation remains defensive against other writes.
    """
    from sqlalchemy.exc import DBAPIError

    from packages.core.models import User
    from packages.core.services.resume_review import ReviewStateError, request_changes

    resume_id = approved_resume_fixtures["approved_resume_id"]
    approval_id = approved_resume_fixtures["approval_id"]

    async def rejected_rework() -> None:
        actor = await session.get(User, demo_user)
        with pytest.raises(ReviewStateError, match="awaiting_approval"):
            await request_changes(
                session, application_id=approved_resume_fixtures["application_id"],
                actor=actor, note="Synthetic rework request after approval",
            )
        approval = await session.get(Approval, approval_id)
        with pytest.raises(DBAPIError, match="requires a pending resume approval and human actor"):
            async with session.begin_nested():
                approval.status = ApprovalStatus.CHANGES_REQUESTED
                await session.flush()
        await session.refresh(approval)
        assert approval.status == ApprovalStatus.APPROVED
        assert approval.decided_by == demo_user and approval.decided_at is not None

    tripwire = TripwireStore(store, rejected_rework)
    produced = await render_resume(
        session, store=tripwire, renderers=renderers, resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    assert tripwire.keys, "the rejected decision must occur after upload"
    artifacts, events = await _durable_record(session, resume_id)
    assert len(produced) == len(artifacts) == len(events) == 1


async def test_a_resume_superseded_mid_render_persists_nothing(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """Race two: the resume moves on while the renderer works.

    The snapshot branch of `_revalidate`: the approval is still granted - it
    just no longer describes this resume. Covered separately because either
    branch could regress on its own, and because an approval that still reads
    `approved` is exactly the state a weaker check would wave through.
    """
    resume_id = approved_resume_fixtures["approved_resume_id"]

    async def regenerate() -> None:
        resume = await session.get(Resume, resume_id)
        resume.version = SUPERSEDING_VERSION
        await session.flush()

    tripwire = TripwireStore(store, regenerate)
    with pytest.raises(StaleApprovalError):
        await render_resume(
            session,
            store=tripwire,
            renderers=renderers,
            resume_id=resume_id,
            allow_non_production_renderers=True,
        )

    approval = await session.get(Approval, approved_resume_fixtures["approval_id"])
    assert approval.status == ApprovalStatus.APPROVED, (
        "this case must fail on the snapshot, not on the approval status"
    )

    assert tripwire.keys, "nothing was uploaded, so the race was never reached"
    artifacts, events = await _durable_record(session, resume_id)
    assert artifacts == []
    assert events == []


async def test_the_orphaned_object_is_recoverable_and_replay_stays_idempotent(
    session, store, renderers, approved_resume_fixtures
) -> None:
    """The documented consequence of uploading before inserting.

    The store fails after writing bytes but before confirming the upload, so
    an object is left behind with nothing pointing at it. That is the
    intended, cheaper failure - the alternative ordering would leave a row
    claiming an artifact a download 404s on - and it is asserted here rather
    than waved away.

    Two properties make it safe: no row ever refers to the orphan, and a
    later render under the unchanged approval reuses the same deterministic key,
    overwrites it with identical bytes and records it, so recovery needs no
    sweeper to have run first. Replay after recovery still writes nothing.
    """
    resume_id = approved_resume_fixtures["approved_resume_id"]
    from packages.services.rendering.store import ArtifactStoreUnavailableError

    async def upload_acknowledgement_lost() -> None:
        raise ArtifactStoreUnavailableError("synthetic failure after upload")

    tripwire = TripwireStore(store, upload_acknowledgement_lost)
    with pytest.raises(ArtifactStoreUnavailableError, match="synthetic failure after upload"):
        await render_resume(
            session,
            store=tripwire,
            renderers=renderers,
            resume_id=resume_id,
            allow_non_production_renderers=True,
        )

    orphan = tripwire.keys[0]
    assert await store.exists(orphan), "the object should already be uploaded"
    artifacts, failed_events = await _durable_record(session, resume_id)
    assert artifacts == [], "no row may point at the orphaned object"
    assert failed_events == [], "an unconfirmed upload must not emit success"

    # Storage recovers; the original human decision never changed.
    approval = await session.get(Approval, approved_resume_fixtures["approval_id"])
    assert approval.status == ApprovalStatus.APPROVED

    produced = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    assert len(produced) == 1
    assert produced[0].storage_key == orphan, (
        "recovery should reuse the deterministic key rather than orphan it twice"
    )
    await store.verify(produced[0].storage_key, sha256=produced[0].sha256)

    rows, events = await _durable_record(session, resume_id)
    assert len(rows) == 1
    assert len(events) == 1, "the successful render emits exactly one rendered event"

    # Replay after recovery: neither a second row nor a second event.
    await session.flush()
    replayed = await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    assert [a.id for a in replayed] == [a.id for a in produced]
    rows_after, events_after = await _durable_record(session, resume_id)
    assert len(rows_after) == 1, "a replay must not write a second artifact row"
    assert len(events_after) == 1, "a replay must not emit a second rendered event"


# ------------------------------------------------------------- the endpoints
async def test_anonymous_callers_cannot_list_or_download(app_client) -> None:
    """M1 still first: 401 before any identifier is looked up, so a missing
    artifact and a forbidden one are indistinguishable from outside."""
    listing = await app_client.get(f"/api/v1/resumes/{uuid7()}/artifacts")
    content = await app_client.get(f"/api/v1/artifacts/{uuid7()}/content")
    assert listing.status_code == 401
    assert content.status_code == 401


async def test_a_missing_artifact_and_a_missing_resume_answer_alike(
    db_app_client,
) -> None:
    """Identical 404s, so identifiers cannot be probed for existence."""
    listing = await db_app_client.get(f"/api/v1/resumes/{uuid7()}/artifacts")
    content = await db_app_client.get(f"/api/v1/artifacts/{uuid7()}/content")
    assert listing.status_code == content.status_code == 404
    assert listing.json()["detail"] == content.json()["detail"] == "artifact not found"


async def test_a_reviewer_may_list_and_download(api_sessions) -> None:
    """M2 read policy unchanged: a reviewer sees what a reviewer reviews."""
    reviewer = await api_sessions("reviewer")
    response = await reviewer.get(f"/api/v1/resumes/{uuid7()}/artifacts")
    assert response.status_code != 403


async def test_the_metadata_endpoint_never_returns_a_storage_key(
    db_app_client, session, store, renderers, approved_resume_fixtures
) -> None:
    resume_id = approved_resume_fixtures["approved_resume_id"]
    await render_resume(
        session,
        store=store,
        renderers=renderers,
        resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    await session.commit()

    response = await db_app_client.get(f"/api/v1/resumes/{resume_id}/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body
    for row in body:
        assert "storage_key" not in row
        assert "bucket" not in row
        assert row["template_version"] == TEMPLATE_VERSION


async def test_downloads_are_rate_limited(api_sessions, monkeypatch) -> None:
    """Authenticated, but not unlimited."""
    from packages.shared.config import get_settings

    monkeypatch.setenv("RATE_LIMIT_ARTIFACT_DOWNLOAD_PER_USER", "2")
    get_settings.cache_clear()
    owner = await api_sessions("owner")
    seen = [
        (await owner.get(f"/api/v1/artifacts/{uuid7()}/content")).status_code
        for _ in range(4)
    ]
    get_settings.cache_clear()
    assert 429 in seen


async def test_csrf_is_irrelevant_to_a_read(db_app_client) -> None:
    """Downloads are GETs; the double-submit check does not apply to them,
    and removing the header must not turn a 404 into a 403."""
    db_app_client.headers.pop(CSRF_HEADER, None)
    response = await db_app_client.get(f"/api/v1/artifacts/{uuid7()}/content")
    assert response.status_code == 404


async def test_approval_fixture_has_human_decision_and_exact_snapshot(
    session, approved_resume_fixtures, demo_user, demo_job,
) -> None:
    from packages.core.models import Application
    from packages.core.services.resume_review import snapshot_matches

    fixture = approved_resume_fixtures
    application = await session.get(Application, fixture["application_id"])
    approval = await session.get(Approval, fixture["approval_id"])
    resume = await session.get(Resume, fixture["approved_resume_id"])
    assert str(application.status) == "approved"
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.decided_by == application.approved_by == demo_user
    assert approval.decided_at is not None and application.approved_at is not None
    assert approval.application_id == application.id == resume.application_id
    assert snapshot_matches(approval.subject_snapshot, resume)
    assert approval.subject_snapshot["job_id"] == str(demo_job)


@pytest.mark.parametrize("role", ["owner", "reviewer"])
@pytest.mark.parametrize("resource", ["missing", "unreadable", "known"])
async def test_store_outage_is_considered_only_for_readable_artifacts(
    api_sessions, session, store, renderers, approved_resume_fixtures,
    monkeypatch, role, resource,
) -> None:
    from apps.api.routers import artifacts as router
    from packages.core.db import get_session
    from packages.services.rendering.store import ArtifactStoreUnavailableError

    client = await api_sessions(role)
    artifact_id = uuid7()
    if resource == "unreadable":
        # An evidence upload is not a resume download under the existing policy.
        session.add(Artifact(id=artifact_id, kind="evidence", resume_id=None,
                             storage_key=f"evidence/{artifact_id}", bucket=store.bucket))
        await session.flush()
    elif resource == "known":
        produced = await render_resume(
            session, store=store, renderers=renderers,
            resume_id=approved_resume_fixtures["approved_resume_id"],
            allow_non_production_renderers=True,
        )
        artifact_id = produced[0].id

    calls = []

    def unavailable(settings):
        calls.append(True)
        raise ArtifactStoreUnavailableError("synthetic storage outage")

    async def database():
        yield session

    monkeypatch.setattr(router, "artifact_store", unavailable)
    app = client._transport.app
    app.dependency_overrides[get_session] = database
    try:
        response = await client.get(f"/api/v1/artifacts/{artifact_id}/content")
    finally:
        app.dependency_overrides.pop(get_session)
    if resource == "known":
        assert response.status_code == 503
        assert response.json()["detail"] == "synthetic storage outage"
        assert calls == [True]
    else:
        assert response.status_code == 404
        assert response.json()["detail"] == "artifact not found"
        assert not calls


@pytest.mark.parametrize("mixed", [False, True])
async def test_rendered_event_exact_public_v2_contract(
    session, store, renderers, approved_resume_fixtures, mixed,
) -> None:
    import json

    from packages.core.events.rendered import ResumeRenderedV2

    resume_id = approved_resume_fixtures["approved_resume_id"]
    selected = renderers
    if mixed:
        await render_resume(
            session, store=store, renderers=renderers, resume_id=resume_id,
            allow_non_production_renderers=True,
        )
        selected = (*renderers, ReferenceTextRenderer(ALT_TEMPLATE_VERSION))
    produced = await render_resume(
        session, store=store, renderers=selected, resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    _, events = await _durable_record(session, resume_id)
    expected = {
        "resume_id": str(resume_id),
        "application_id": str(approved_resume_fixtures["application_id"]),
        "resume_version": approved_resume_fixtures["approved_version"],
        "approval_id": str(approved_resume_fixtures["approval_id"]),
        "artifacts": [{"artifact_id": str(a.id), "kind": a.kind,
                       "template_version": a.template_version,
                       "size_bytes": a.size_bytes, "sha256": a.sha256} for a in produced],
    }
    assert len(events) == (2 if mixed else 1)
    event = next(e for e in events if len(e.payload["artifacts"]) == len(produced))
    await session.refresh(event)  # Read the actual persisted JSONB, not a constructed fixture.
    decoded = json.loads(json.dumps(event.payload))
    assert decoded == expected
    parsed = ResumeRenderedV2.model_validate_json(json.dumps(decoded))
    assert parsed.model_dump(mode="json") == expected
    assert event.version == 2
    assert event.aggregate_id == resume_id
    assert event.correlation_id == approved_resume_fixtures["application_id"]
    await render_resume(
        session, store=store, renderers=selected, resume_id=resume_id,
        allow_non_production_renderers=True,
    )
    _, replay_events = await _durable_record(session, resume_id)
    assert {e.id for e in replay_events} == {e.id for e in events}
