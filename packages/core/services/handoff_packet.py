"""M10 Phase 2 - the deterministic human-handoff packet.

Follows the exact crash-ordering discipline `packages.core.services.artifacts.
render_resume` established for M8: build the bytes -> upload -> verify the
checksum on the way back -> insert the row. A crash between upload and insert
leaves an orphan object in the store - cheap, sweepable, harmless. The
opposite order would leave a row claiming bytes that a download 404s on.

**Binding without a migration.** `artifacts` has no column for "which
cover-letter version" or "which answers content" a packet was built from -
only `resume_id`/`resume_version`, which M8 already uses for renders. Rather
than add columns, the storage key itself carries the full binding: it is
deterministically derived from the application id, the resume id+version, the
cover-letter id+version (or its absence), and a content digest of the current
answers. Two calls with identical source material always address the same
key (idempotent, no duplicate object); any change to any source - a new
resume version, a new cover-letter version, an edited answer - produces a
different key, so a lookup for "the packet matching current state" can never
return one built from stale material by construction, without reading back
anything this schema cannot express. `resume_id`/`resume_version` are also
populated on the row itself, for the same free, already-existing columns M8
uses - readable and queryable, though the key is what is authoritative.

Nothing here chooses a submission method or sends anything anywhere. The
checklist text is the one place a human reads what M10 actually decided, and
it says so - `submission_ready` explicitly states automated submission is not
implemented until M11, so packet existence is never mistaken for permission
to auto-submit.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from packages.core.models import (
    Application,
    ApplicationQuestion,
    ApplicationStatus,
    Artifact,
    CoverLetter,
    Job,
    Resume,
)
from packages.core.services.applications import existing_questions
from packages.core.services.cover_letters import latest_cover_letter
from packages.core.services.resume_review import latest_tailored_resume
from packages.services.ingestion.policy import extract_domain
from packages.shared.errors import ConflictError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.services.rendering.store import ArtifactStore

log = get_logger(__name__)

PACKET_KIND = "handoff_packet"
PACKET_CONTENT_TYPE = "application/zip"

#: The three post-readiness states a packet may exist for. Not gated on
#: `manual_handoff` alone - a human may want the packet for `submission_ready`
#: (preview, or apply manually rather than wait for M11) or `policy_blocked`
#: (apply manually outside the system) just as much.
PACKET_ELIGIBLE_STATUSES = frozenset(
    {
        ApplicationStatus.SUBMISSION_READY,
        ApplicationStatus.MANUAL_HANDOFF,
        ApplicationStatus.POLICY_BLOCKED,
    }
)


class ResumeNotRenderedError(ConflictError):
    """No rendered resume_pdf artifact exists yet for the current resume version.

    Real, if rare: `resume.render.requested` fires the instant a human
    approves the resume (resume_review.approve), but the render worker
    processing it is asynchronous - a packet requested in the narrow window
    before that finished has nothing to bind to yet. Refused rather than
    built without one; a handoff packet with no resume is not a packet.
    """


class NoCoverLetterError(ConflictError):
    """No cover letter has been generated for this application."""


@dataclass(frozen=True)
class PacketSource:
    """Exactly what one packet was built from - the values baked into its key."""

    resume: Resume
    resume_artifact: Artifact
    cover_letter: CoverLetter
    questions: list[ApplicationQuestion]
    answers_digest: str


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _answers_digest(questions: list[ApplicationQuestion]) -> str:
    """A content digest of the current answers, not an identifier - there is
    no `version` for `application_answers` the way there is for a resume or
    cover letter, because a row can be edited in place after any approval."""
    payload = [
        {
            "question_id": str(q.id),
            "value": q.answer.value if q.answer else None,
            "value_structured": q.answer.value_structured if q.answer else {},
            "source": str(q.answer.source) if q.answer else None,
        }
        for q in questions
    ]
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16]


def _packet_storage_key(
    *,
    application_id: UUID,
    resume_id: UUID,
    resume_version: int,
    cover_letter_id: UUID,
    cover_letter_version: int,
    answers_digest: str,
) -> str:
    """Deterministic and content-addressed - see the module docstring."""
    return (
        f"applications/{application_id}/packet/"
        f"r-{resume_id}-v{resume_version}/"
        f"c-{cover_letter_id}-v{cover_letter_version}/"
        f"a-{answers_digest}/packet.zip"
    )


async def _load_source(session: AsyncSession, application: Application) -> PacketSource:
    resume = await latest_tailored_resume(session, application.id)
    if resume is None:
        raise ResumeNotRenderedError(f"no resume exists for application {application.id}")
    resume_artifact = (
        await session.execute(
            select(Artifact).where(
                Artifact.resume_id == resume.id, Artifact.kind == "resume_pdf"
            )
        )
    ).scalar_one_or_none()
    if resume_artifact is None:
        raise ResumeNotRenderedError(
            f"resume {resume.id} v{resume.version} has no rendered PDF artifact yet"
        )
    cover_letter = await latest_cover_letter(session, application.id)
    if cover_letter is None:
        raise NoCoverLetterError(f"no cover letter exists for application {application.id}")
    questions = await existing_questions(session, application.id)
    return PacketSource(
        resume=resume,
        resume_artifact=resume_artifact,
        cover_letter=cover_letter,
        questions=questions,
        answers_digest=_answers_digest(questions),
    )


def _checklist_text(
    *, status: ApplicationStatus, method: str, domain: str, blocked_reason: str | None
) -> str:
    lines = [
        "JobForge handoff packet",
        "========================",
        "",
    ]
    if status is ApplicationStatus.MANUAL_HANDOFF:
        lines += [
            "STATUS: Human action required.",
            "",
            f"JobForge has prepared this application for {domain}, but this "
            "channel requires a human to submit it - JobForge does not and "
            "will not submit it automatically. Use the enclosed resume, "
            "cover letter and answers to apply directly.",
        ]
    elif status is ApplicationStatus.POLICY_BLOCKED:
        lines += [
            "STATUS: Automated submission blocked by policy.",
            "",
            f"JobForge's automated submission to {domain} is blocked by this "
            "site's recorded policy"
            + (f" ({blocked_reason})" if blocked_reason else "")
            + ". JobForge will not attempt to submit this application. If "
            "you wish to apply, you must do so yourself, outside JobForge.",
        ]
    elif status is ApplicationStatus.SUBMISSION_READY:
        lines += [
            "STATUS: Ready - awaiting a permitted automated channel.",
            "",
            f"Policy permits submitting to {domain} via {method}, but "
            "JobForge does not yet implement automated submission for any "
            "channel (that capability is not implemented until a later "
            "milestone). No automated submission has been attempted or "
            "will be attempted by this version of JobForge. This packet's "
            "existence is not permission to auto-submit - if you wish to "
            "apply now, do so yourself using the enclosed materials.",
        ]
    else:  # pragma: no cover - defensive; PACKET_ELIGIBLE_STATUSES guards this
        lines += [f"STATUS: {status}."]
    lines += ["", f"Target domain: {domain}", f"Configured method: {method}"]
    return "\n".join(lines) + "\n"


def _job_metadata_text(job: Job) -> str:
    fields = {
        "title": job.title,
        "normalized_title": job.normalized_title,
        "seniority": job.seniority,
        "location_city": job.location_city,
        "location_state": job.location_state,
        "location_country": job.location_country,
        "is_remote": job.is_remote,
        "employment_type": job.employment_type,
        "url": job.url,
    }
    return _canonical(fields)


def _answers_text(questions: list[ApplicationQuestion]) -> str:
    if not questions:
        return "No screening questions were prepared for this application.\n"
    lines = [
        "Application questions and prepared answers",
        "===========================================",
        "",
    ]
    for q in questions:
        answer = q.answer
        lines.append(f"Q: {q.question_text}")
        lines.append(f"   class: {q.question_class}")
        if answer is None or answer.value is None:
            lines.append("   A: (left blank - requires human input)")
        else:
            lines.append(f"   A: {answer.value}  (source: {answer.source})")
        lines.append("")
    return "\n".join(lines)


def build_packet_zip(
    *,
    resume_pdf: bytes,
    cover_letter_text: str,
    answers_text: str,
    job_metadata: str,
    checklist: str,
) -> bytes:
    """Pure: identical inputs produce identical bytes (fixed member order,
    no timestamps beyond zipfile's own fixed epoch default)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in (
            ("00-CHECKLIST.txt", checklist),
            ("resume.pdf", resume_pdf),
            ("cover_letter.txt", cover_letter_text),
            ("answers.txt", answers_text),
            ("job.json", job_metadata),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            zf.writestr(info, data)
    return buffer.getvalue()


async def _existing_packet(
    session: AsyncSession, application_id: UUID, key: str
) -> Artifact | None:
    return (
        await session.execute(
            select(Artifact).where(
                Artifact.application_id == application_id,
                Artifact.kind == PACKET_KIND,
                Artifact.storage_key == key,
            )
        )
    ).scalar_one_or_none()


async def generate_handoff_packet(
    session: AsyncSession,
    *,
    application: Application,
    store: ArtifactStore,
    method: str,
    blocked_reason: str | None,
) -> Artifact:
    """Build (or reuse) the packet matching the application's current
    resume/cover-letter/answers state. Idempotent by storage key - a replay
    with unchanged source material returns the existing row untouched, never
    a second upload or a second row.
    """
    if application.status not in PACKET_ELIGIBLE_STATUSES:
        raise ConflictError(
            f"a handoff packet may only be generated from "
            f"{sorted(str(s) for s in PACKET_ELIGIBLE_STATUSES)}, not {application.status}"
        )
    source = await _load_source(session, application)
    key = _packet_storage_key(
        application_id=application.id,
        resume_id=source.resume.id,
        resume_version=source.resume.version,
        cover_letter_id=source.cover_letter.id,
        cover_letter_version=source.cover_letter.version,
        answers_digest=source.answers_digest,
    )
    existing = await _existing_packet(session, application.id, key)
    if existing is not None:
        return existing

    job = await session.get(Job, application.job_id)
    assert job is not None  # application.job_id is NOT NULL with FK RESTRICT

    resume_bytes = await store.get(source.resume_artifact.storage_key)
    if source.resume_artifact.sha256:
        await store.verify(source.resume_artifact.storage_key, sha256=source.resume_artifact.sha256)

    checklist = _checklist_text(
        status=application.status,
        method=method,
        domain=_domain(job),
        blocked_reason=blocked_reason,
    )
    data = build_packet_zip(
        resume_pdf=resume_bytes,
        cover_letter_text=source.cover_letter.body_text or "",
        answers_text=_answers_text(source.questions),
        job_metadata=_job_metadata_text(job),
        checklist=checklist,
    )
    sha256 = hashlib.sha256(data).hexdigest()

    await store.put(key, data, content_type=PACKET_CONTENT_TYPE)
    await store.verify(key, sha256=sha256)

    artifact = Artifact(
        id=uuid7(),
        kind=PACKET_KIND,
        application_id=application.id,
        resume_id=source.resume.id,
        resume_version=source.resume.version,
        storage_key=key,
        bucket=store.bucket,
        content_type=PACKET_CONTENT_TYPE,
        size_bytes=len(data),
        sha256=sha256,
    )
    session.add(artifact)
    await session.flush()
    log.info(
        "handoff_packet.generated",
        application_id=str(application.id),
        artifact_id=str(artifact.id),
        resume_version=source.resume.version,
        cover_letter_version=source.cover_letter.version,
    )
    return artifact


def _domain(job: Job) -> str:
    return extract_domain(job.url)


async def current_packet(session: AsyncSession, application_id: UUID) -> Artifact | None:
    """The most recently generated packet for this application, if any -
    what `GET /applications/{id}/packet` serves. Does not regenerate."""
    return (
        await session.execute(
            select(Artifact)
            .where(Artifact.application_id == application_id, Artifact.kind == PACKET_KIND)
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
