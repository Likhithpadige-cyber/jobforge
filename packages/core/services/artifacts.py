"""Render an approved resume into durable, private artifacts.

The ordering in `render_resume` is the whole point of this module, so it is
stated once here and then followed exactly:

    revalidate -> render -> upload -> read back and compare -> REVALIDATE
    AGAIN -> insert the row -> emit

Two revalidations, not one. The first refuses work that was never authorised.
The second runs immediately before the row is written, because rendering is
not instantaneous and a human may have requested changes in the meantime: an
artifact is a document someone will send to an employer, and it must describe
the version that was approved at the moment it became durable, not the one
that was approved when the job was picked up.

Upload before insert, never the other way round. A crash between them leaves
an orphan object - garbage, sweepable, harmless. The opposite order would
leave a row claiming an artifact that a download 404s on, which is a record
that lies about what exists. The readback closes the remaining gap: a
truncated or corrupted upload fails the checksum comparison before anything
in the database refers to it.

Nothing here writes resume content. It reads `resumes.content`, hands it to a
renderer, and stores bytes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events import EventEnvelope, record_event
from packages.core.events.rendered import ResumeRenderedV2
from packages.core.models import Application, Approval, Artifact, Resume
from packages.core.models.enums import ApprovalKind, ApprovalStatus
from packages.core.services.resume_export import approved_export, frozen_material_digest
from packages.core.services.resume_review import snapshot_matches
from packages.services.rendering import (
    RenderedArtifact,
    RendererUnavailableError,
    ResumeRenderer,
    build_document,
    storage_key,
)
from packages.services.rendering.store import ArtifactStore
from packages.shared.errors import ConflictError, NotFoundError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

log = get_logger(__name__)

#: Statuses in which an approved resume may be rendered. A resume that has
#: gone back for changes is not one of them: the approval that authorised it
#: has been superseded, and rendering it would hand a human a document the
#: workflow has already moved past.
RENDERABLE_APPLICATION_STATUSES = frozenset(
    {
        "approved",
        "submission_ready",
        "submitting",
        "submitted",
        "manual_handoff",
    }
)


class NotApprovedError(ConflictError):
    """No live approval authorises rendering this exact resume version."""

    title = "Resume Not Approved"
    error_type = "https://jobforge.local/errors/resume-not-approved"


class StaleApprovalError(ConflictError):
    """The approval stopped describing this resume while it was rendering."""

    title = "Approval Superseded"
    error_type = "https://jobforge.local/errors/approval-superseded"


class RenderContext:
    """The rows a render depends on, loaded once and revalidated cheaply."""

    def __init__(self, resume: Resume, application: Application, approval: Approval) -> None:
        self.resume = resume
        self.application = application
        self.approval = approval
        self.material_digest = frozen_material_digest(resume, approval.subject_snapshot or {})


async def _authorising_approval(
    session: AsyncSession, *, resume: Resume, application: Application
) -> Approval:
    """The granted approval whose subject is exactly this resume version.

    `snapshot_matches` is reused rather than reimplemented. It already
    requires both the id and the version to match, and its docstring states
    why: matching on id alone would let a regenerated version inherit a
    decision made about its predecessor. M8 needs precisely that guarantee,
    and a second implementation of it would be a second thing to keep right.
    """
    rows = (
        await session.scalars(
            select(Approval)
            .where(
                Approval.application_id == application.id,
                Approval.kind == ApprovalKind.RESUME,
                Approval.status == ApprovalStatus.APPROVED,
            )
            .order_by(Approval.decided_at.desc())
        )
    ).all()
    for approval in rows:
        if snapshot_matches(approval.subject_snapshot or {}, resume):
            return approval
    raise NotApprovedError("This resume version has no granted approval")


async def load_context(session: AsyncSession, resume_id: UUID) -> RenderContext:
    """Load and validate everything rendering depends on, or refuse.

    Runs before any renderer is constructed, so an unauthorised request never
    reaches the point of spending CPU on a document it may not have.
    """
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise NotFoundError("resume not found")
    if resume.verification_status != "passed":
        # The fabrication tripwire. A resume that failed verification is
        # reviewed by a human, never rendered into something sendable.
        raise NotApprovedError("Only a verified resume may be rendered")
    if resume.application_id is None:
        raise NotApprovedError("Only a tailored resume bound to an application is rendered")

    application = await session.get(Application, resume.application_id)
    if application is None:
        raise NotFoundError("resume not found")
    if str(application.status) not in RENDERABLE_APPLICATION_STATUSES:
        raise NotApprovedError("This application is not in an approved state")

    approval = await _authorising_approval(session, resume=resume, application=application)
    return RenderContext(resume=resume, application=application, approval=approval)


async def _revalidate(session: AsyncSession, context: RenderContext) -> None:
    """The second check, immediately before the row is written.

    Refreshes from the database rather than trusting the objects loaded
    before rendering started. If a reviewer requested changes while the
    renderer was working, the approval no longer matches and the artifact is
    abandoned - the uploaded object becomes an orphan, which is the cheap
    failure, and nothing durable claims the stale version was rendered.
    """
    await session.refresh(context.application, with_for_update=True)
    if str(context.application.status) not in RENDERABLE_APPLICATION_STATUSES:
        raise StaleApprovalError("Application is no longer in an approved state")
    await session.refresh(context.resume, with_for_update=True)
    await session.refresh(context.approval, with_for_update=True)
    if context.approval.status != ApprovalStatus.APPROVED:
        raise StaleApprovalError("The authorising approval is no longer granted")
    if context.resume.verification_status != "passed":
        raise StaleApprovalError("Resume verification is no longer passed")
    if (
        frozen_material_digest(context.resume, context.approval.subject_snapshot or {})
        != context.material_digest
    ):
        raise StaleApprovalError("The approved export inputs changed while rendering")
    if not snapshot_matches(context.approval.subject_snapshot or {}, context.resume):
        raise StaleApprovalError("The approval no longer describes this resume version")


async def _existing(
    session: AsyncSession, *, resume_id: UUID, kind: str, template_version: str
) -> Artifact | None:
    return (
        await session.scalars(
            select(Artifact).where(
                Artifact.resume_id == resume_id,
                Artifact.kind == kind,
                Artifact.template_version == template_version,
            )
        )
    ).first()


def _select_renderers(
    renderers: tuple[ResumeRenderer, ...], *, allow_non_production: bool
) -> tuple[ResumeRenderer, ...]:
    """Refuse anything that must not serve a real download.

    A non-production renderer is admitted only when the caller asked for one
    by name. That is what stops a test double becoming the thing that renders
    a real resume simply by being the only renderer that imports cleanly.
    """
    chosen: list[ResumeRenderer] = []
    for renderer in renderers:
        if not renderer.production and not allow_non_production:
            raise RendererUnavailableError(f"{renderer.kind}: refusing a non-production renderer")
        if not renderer.available() or not renderer.implemented:
            raise RendererUnavailableError(
                f"{renderer.kind}: renderer dependency or implementation is unavailable"
            )
        chosen.append(renderer)
    return tuple(chosen)


async def render_resume(
    session: AsyncSession,
    *,
    store: ArtifactStore,
    renderers: tuple[ResumeRenderer, ...],
    resume_id: UUID,
    causation_id: UUID | None = None,
    allow_non_production_renderers: bool = False,
) -> list[Artifact]:
    """Render, store and record. Idempotent, and safe to replay.

    Returns the artifacts for this resume, whether they were produced now or
    already existed. A replayed `resume.render.requested` therefore costs one
    checksum comparison and writes nothing.
    """
    context = await load_context(session, resume_id)
    selected = _select_renderers(renderers, allow_non_production=allow_non_production_renderers)
    document = build_document(
        resume_id=str(context.resume.id),
        resume_version=context.resume.version,
        content=context.resume.content or {},
        export_data=approved_export(context.approval.subject_snapshot, context.resume)
        if any(r.production for r in selected)
        else None,
    )

    produced: list[Artifact] = []
    created: list[Artifact] = []
    for renderer in selected:
        existing = await _existing(
            session,
            resume_id=context.resume.id,
            kind=renderer.kind,
            template_version=renderer.template_version,
        )
        if existing is not None:
            # Already rendered under this template. Not re-uploaded and not
            # re-rendered: the unique index says there is one artifact per
            # (resume, format, template), and honouring that here means a
            # redelivery cannot produce a second set of bytes for the same row.
            if (
                existing.approval_id != context.approval.id
                or existing.resume_version != context.resume.version
            ):
                raise StaleApprovalError("Existing artifact belongs to another approval or version")
            await _revalidate(session, context)
            produced.append(existing)
            continue

        rendered = RenderedArtifact(
            kind=renderer.kind,
            content_type=renderer.content_type,
            extension=renderer.extension,
            template_version=renderer.template_version,
            data=renderer.render(document),
        )
        key = storage_key(
            resume_id=context.resume.id,
            template_version=renderer.template_version,
            extension=renderer.extension,
        )

        # Upload, then prove the bytes landed intact, then revalidate, then
        # record. Each step is ordered so a crash after it leaves something
        # harmless rather than something misleading.
        await store.put(key, rendered.data, content_type=rendered.content_type)
        await store.verify(key, sha256=rendered.sha256)
        await _revalidate(session, context)

        artifact = Artifact(
            id=uuid7(),
            kind=rendered.kind,
            application_id=context.application.id,
            resume_id=context.resume.id,
            resume_version=context.resume.version,
            approval_id=context.approval.id,
            template_version=rendered.template_version,
            storage_key=key,
            bucket=store.bucket,
            content_type=rendered.content_type,
            size_bytes=rendered.size_bytes,
            sha256=rendered.sha256,
        )
        session.add(artifact)
        await session.flush()
        produced.append(artifact)
        created.append(artifact)

        log.info(
            "artifact.rendered",
            artifact_id=str(artifact.id),
            resume_id=str(context.resume.id),
            resume_version=context.resume.version,
            kind=rendered.kind,
            template_version=rendered.template_version,
            size_bytes=rendered.size_bytes,
        )

    # Emit only when this invocation actually persisted something. Creation
    # is tracked as it happens rather than inferred afterwards: an artifact
    # returned by the `_existing` short-circuit is a row loaded from the
    # database, so `a.id is not None` is true of it too, and inferring from
    # that made a pure replay emit a second success event for work it had not
    # done.
    #
    # The payload still describes every artifact the resume now has, not only
    # the ones created here. `resume.rendered` tells a consumer what is
    # available to act on, and a mixed invocation that reported only the new
    # format would understate what exists.
    if created:
        await record_event(
            session,
            EventEnvelope(
                type="resume.rendered",
                version=2,
                aggregate_type="resume",
                aggregate_id=context.resume.id,
                correlation_id=context.application.id,
                causation_id=causation_id,
                actor_type="agent",
                payload=ResumeRenderedV2.model_validate(
                    {
                        "resume_id": str(context.resume.id),
                        "application_id": str(context.application.id),
                        "resume_version": context.resume.version,
                        "approval_id": str(context.approval.id),
                        "artifacts": [
                            {
                                "artifact_id": str(a.id),
                                "kind": a.kind,
                                "template_version": a.template_version,
                                "size_bytes": a.size_bytes,
                                "sha256": a.sha256,
                            }
                            for a in produced
                        ],
                    }
                ).model_dump(mode="json"),
            ),
        )
    return produced


async def artifact_payloads(session: AsyncSession, resume_id: UUID) -> list[dict[str, Any]]:
    """Metadata for the artifacts of one resume. No bytes, no storage key.

    The storage key is deliberately absent from anything a client sees. It is
    an internal address; publishing it invites someone to try it directly and
    tells them the shape of the namespace.
    """
    rows = (
        await session.scalars(
            select(Artifact)
            .where(Artifact.resume_id == resume_id)
            .order_by(Artifact.kind, Artifact.created_at)
        )
    ).all()
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "sha256": a.sha256,
            "resume_id": a.resume_id,
            "resume_version": a.resume_version,
            "approval_id": a.approval_id,
            "template_version": a.template_version,
            "created_at": a.created_at,
        }
        for a in rows
    ]
