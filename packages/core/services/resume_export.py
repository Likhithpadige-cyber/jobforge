"""Frozen owner-reviewed body details, gated by current evidence validity at export time."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    Certification,
    Education,
    Experience,
    Profile,
    ProfileFact,
    Resume,
    User,
)
from packages.services.rendering.text import export_url, single_line
from packages.shared.clock import utc_today
from packages.shared.errors import ConflictError, NotFoundError


def _canonical_text(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {k: _canonical_text(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical_text(v) for v in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical_text(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def export_binding(resume: Resume, document: dict[str, Any]) -> str:
    return digest(
        {
            "resume_id": str(resume.id),
            "version": resume.version,
            "content": resume.content,
            "document": document,
        }
    )


def frozen_material_digest(resume: Resume, snapshot: dict[str, Any]) -> str:
    """Identity of the exact inputs used to produce bytes, independent of later reloads."""
    return digest(
        {
            "resume_id": str(resume.id),
            "version": resume.version,
            "application_id": str(resume.application_id),
            "profile_id": str(resume.profile_id),
            "kind": resume.kind,
            "verification_status": resume.verification_status,
            "content": resume.content,
            "approval_snapshot": snapshot,
        }
    )


async def validate_current_evidence(
    session: AsyncSession,
    resume: Resume,
    *,
    lock: bool = False,
    extra_fact_ids: set[str] | None = None,
) -> None:
    """Frozen consent cannot keep withdrawn, expired or unverified evidence active."""
    cited = {
        str(fid)
        for section in (resume.content or {}).values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    cited.update(extra_fact_ids or set())
    if not cited:
        return
    query = select(ProfileFact).where(ProfileFact.profile_id == resume.profile_id)
    if lock:
        query = query.with_for_update()
    facts = list((await session.scalars(query.execution_options(populate_existing=True))).all())
    by_id = {str(f.id): f for f in facts}
    today = utc_today()
    for fid in cited:
        fact = by_id.get(fid)
        if (
            fact is None
            or str(fact.status) != "verified"
            or fact.deleted_at is not None
            or (fact.valid_from and fact.valid_from > today)
            or (fact.valid_to and fact.valid_to < today)
        ):
            raise ConflictError("Resume cites inactive evidence; regenerate before export")


async def validate_export_records(session: AsyncSession, snapshot: dict[str, Any]) -> None:
    """A frozen credential must not continue to be presented as currently held."""
    from uuid import UUID

    today = utc_today()
    for record in snapshot.get("export_document", {}).get("records", []):
        if record.get("source_table") != "certifications":
            continue
        credential = await session.get(
            Certification, UUID(record["source_id"]), populate_existing=True, with_for_update=True
        )
        if (
            credential is None
            or credential.deleted_at is not None
            or credential.status != "active"
            or (credential.expires_date and credential.expires_date < today)
        ):
            raise ConflictError("Export certification evidence is no longer active")


async def preview_export(session: AsyncSession, resume: Resume, actor: User) -> dict[str, Any]:
    # Lock the profile: all supported source mutations bump its version and take
    # this lock. Approval compares exactly the preview, including source version.
    profile = await session.scalar(
        select(Profile)
        .where(Profile.id == resume.profile_id, Profile.user_id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor.role != "owner" or profile is None:
        raise NotFoundError("resume export not found")
    facts = list(
        (
            await session.scalars(
                select(ProfileFact)
                .where(ProfileFact.profile_id == profile.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    await validate_current_evidence(session, resume)
    cited = {
        str(fid)
        for section in (resume.content or {}).values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    today = utc_today()
    records: list[dict[str, Any]] = []
    definitions: tuple[
        tuple[type[Experience] | type[Education] | type[Certification], str, str, tuple[str, ...]],
        ...,
    ] = (
        (
            Experience,
            "experience",
            "experiences",
            ("title", "company_name", "start_date", "end_date", "location"),
        ),
        (
            Education,
            "education",
            "educations",
            ("institution", "degree", "field_of_study", "start_date", "end_date", "location"),
        ),
        (
            Certification,
            "certifications",
            "certifications",
            ("name", "issuer", "issued_date", "expires_date", "status", "credential_url"),
        ),
    )
    for model, section, table, fields in definitions:
        rows = list(
            (
                await session.scalars(
                    select(model)
                    .where(model.profile_id == profile.id, model.deleted_at.is_(None))
                    .order_by(model.display_order, model.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        for source_row in rows:
            row = cast(Experience | Education | Certification, source_row)
            if isinstance(row, Certification) and (
                row.status != "active" or (row.expires_date and row.expires_date < today)
            ):
                continue
            values = {
                key: value.isoformat() if hasattr(value, "isoformat") else value
                for key in fields
                if (value := getattr(row, key)) is not None
            }
            if isinstance(row, Experience) and row.end_date is None:
                values["end_date"] = "Present"
            records.append(
                {
                    "section": section,
                    "source_table": table,
                    "source_id": str(row.id),
                    "fields": values,
                    "fact_ids": sorted(
                        str(f.id)
                        for f in facts
                        if str(f.id) in cited and f.source_table == table and f.source_id == row.id
                    ),
                }
            )
    contact = {
        key: value
        for key, value in {
            "name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": ", ".join(
                v
                for v in (profile.location_city, profile.location_state, profile.location_country)
                if v
            ),
        }.items()
        if value
    }
    links = {
        str(k): str(v)
        for k, v in (profile.links or {}).items()
        if isinstance(v, str) and v.startswith(("https://", "http://"))
    }
    document: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": str(profile.id),
        "profile_version": profile.version,
        "contact": contact,
        "links": links,
        "records": records,
    }
    try:
        document["contact"] = {k: single_line(str(v)) for k, v in contact.items()}
        document["links"] = {k: export_url(v) for k, v in links.items()}
        for record in records:
            record["fields"] = {k: single_line(str(v)) for k, v in record["fields"].items()}
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    # Validate association and renderability before asking a person to approve.
    from packages.services.rendering.document import MalformedResumeContentError, build_document
    from packages.services.rendering.pdf import validate_text

    try:
        doc = build_document(
            resume_id=str(resume.id),
            resume_version=resume.version,
            content=resume.content,
            export_data=document,
        )
    except MalformedResumeContentError as exc:
        raise ConflictError(str(exc)) from exc
    validate_text("\n".join(doc.all_text()))
    validate_text("\n".join(section.heading for section in doc.sections))
    return {
        "document": document,
        "digest": export_binding(resume, document),
        "rendered_lines": list(doc.all_text()),
    }


def approved_export(snapshot: dict[str, Any], resume: Resume) -> dict[str, Any]:
    document = snapshot.get("export_document")
    if (
        not isinstance(document, dict)
        or not snapshot.get("export_owner_id")
        or snapshot.get("export_digest") != export_binding(resume, document)
    ):
        raise ConflictError(
            "Complete export requires fresh owner approval of contact and structured details"
        )
    # Revalidate older frozen snapshots too: manual/cached artifact paths do
    # not necessarily rebuild a document through a renderer before reuse.
    from packages.services.rendering.document import MalformedResumeContentError, build_document

    try:
        build_document(
            resume_id=str(resume.id),
            resume_version=resume.version,
            content=resume.content or {},
            export_data=document,
        )
    except MalformedResumeContentError as exc:
        raise ConflictError(
            "Approved export details are invalid; review a new resume version"
        ) from exc
    return document
