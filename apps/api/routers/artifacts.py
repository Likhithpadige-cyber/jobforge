"""Artifact metadata and authenticated downloads.

Bytes stream through this API rather than through a presigned URL. A
presigned URL is a bearer token for one person's resume: once minted it works
for whoever holds it until it expires, with no session, no role check and no
audit. Streaming keeps authorization on every single request, and costs a
system with one user nothing worth measuring.

Both routes sit behind the M1 router gate, so an anonymous caller is refused
before any identifier is looked up and a missing artifact and a forbidden one
are indistinguishable from outside.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from apps.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    artifact_download_limit,
    artifact_store,
    authenticated_api,
    rate_limited,
)
from apps.api.schemas.artifacts import ArtifactRead
from packages.core.models import Approval, Artifact, Resume
from packages.core.services.artifacts import artifact_payloads
from packages.services.rendering.keys import download_filename
from packages.services.rendering.protocol import ALLOWED_CONTENT_TYPES
from packages.services.rendering.store import ArtifactIntegrityError
from packages.shared.errors import NotFoundError
from packages.shared.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["artifacts"], dependencies=[Depends(authenticated_api)])

#: Deliberately identical for "no such artifact" and "not a readable one".
#: A distinct message would let a caller probe which identifiers exist.
_MISSING = "artifact not found"


@router.get(
    "/resumes/{resume_id}/artifacts",
    response_model=list[ArtifactRead],
    summary="Rendered artifacts for one resume",
)
async def list_resume_artifacts(resume_id: UUID, session: DbSession) -> Any:
    """Metadata only. Owner and reviewer alike, matching M2's read policy.

    A reviewer who may read a resume may see what was rendered from it;
    withholding the metadata would not protect anything the resume itself
    does not already expose.
    """
    if await session.get(Resume, resume_id) is None:
        raise NotFoundError(_MISSING)
    return await artifact_payloads(session, resume_id)


@router.get(
    "/artifacts/{artifact_id}/content",
    summary="Download one rendered artifact",
    response_class=Response,
    dependencies=[Depends(rate_limited(artifact_download_limit))],
)
async def download_artifact(
    artifact_id: UUID, session: DbSession, settings: AppSettings, user: CurrentUser
) -> Response:
    """The bytes, with the checksum re-checked on the way out.

    Three things happen in a deliberate order. The row is read, so the
    response describes what the database recorded rather than what the object
    store happens to hold. The content type is validated against the
    allowlist rather than echoed, so a row that somehow carried a hostile
    value cannot turn into a `text/html` response the browser executes. And
    the bytes are re-hashed and compared before they are returned, so a
    truncated or altered object fails loudly instead of being handed to
    someone about to send it to an employer.
    """
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.resume_id is None:
        raise NotFoundError(_MISSING)
    approval = (
        await session.get(
            Approval, artifact.approval_id, with_for_update=True, populate_existing=True
        )
        if artifact.approval_id
        else None
    )
    contact_snapshot = approval is not None and "export_document" in (
        approval.subject_snapshot or {}
    )
    from packages.services.rendering.protocol import LEGACY_REVIEWER_TEMPLATES

    resume = await session.get(Resume, artifact.resume_id)
    if resume is None:
        raise NotFoundError(_MISSING)
    if contact_snapshot or artifact.template_version not in LEGACY_REVIEWER_TEMPLATES:
        from packages.core.models import Profile

        profile = await session.get(Profile, resume.profile_id) if resume else None
        if user.role != "owner" or profile is None or profile.user_id != user.id:
            raise NotFoundError(_MISSING)
    if (
        artifact.template_version not in LEGACY_REVIEWER_TEMPLATES
        and not contact_snapshot
        and artifact.content_type
        in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    ):
        from packages.shared.errors import ConflictError

        raise ConflictError("Contact export lacks a bound owner approval and evidence fingerprints")
    from packages.core.services.resume_export import (
        approved_export,
        approved_fingerprints,
        validate_current_evidence,
        validate_export_records,
    )

    await validate_current_evidence(
        session,
        resume,
        lock=True,
        fingerprints=approved_fingerprints(approval.subject_snapshot or {}) if approval else None,
        approved_at=approval.decided_at if approval else None,
    )
    if approval is not None:
        if contact_snapshot:
            from packages.shared.errors import ConflictError

            if str(approval.status) != "approved":
                raise ConflictError("Export approval is no longer granted")
            approved_export(approval.subject_snapshot, resume)
        await validate_export_records(session, approval.subject_snapshot or {})
    if artifact.content_type not in ALLOWED_CONTENT_TYPES:
        # Not served, not guessed. The row is wrong and that is a defect.
        log.error("artifact.content_type_rejected", artifact_id=str(artifact_id))
        raise ArtifactIntegrityError("artifact has an unsupported content type")

    # Store construction can fail. Resolve the readable DB resource first so
    # absent/unreadable identifiers remain indistinguishable without storage.
    store = artifact_store(settings)
    data = await store.get(artifact.storage_key)
    if artifact.sha256 and hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ArtifactIntegrityError("artifact checksum mismatch")

    filename = download_filename(
        application_id=artifact.application_id or artifact.resume_id,
        version=artifact.resume_version or 0,
        extension=artifact.kind.rsplit("_", 1)[-1],
    )
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            # The filename is built from identifiers only; `download_filename`
            # has no branch that can read a profile.
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Private by default, and never cached by an intermediary.
            "Cache-Control": "private, no-store",
        },
    )
